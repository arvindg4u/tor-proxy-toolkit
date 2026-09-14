import json
import time
import uuid
from fastapi import HTTPException, Request
from src.core.constants import Constants
from src.conversion.tool_names import from_upstream_name
from src.core.stats import stats
from src.models.claude import ClaudeMessagesRequest


def _parse_chat_item(item):
    """Normalize one upstream chat-stream item to ("chunk", dict | None).

    Returns ("chunk", chunk_dict), ("done", None), or ("ignore", None).
    Phase 3 dict protocol: ``{"type": "chunk", "chunk": {...}}`` /
    ``{"type": "done"}`` (also accepts bare chunk dicts). Legacy
    pre-serialized ``"data: ..."`` strings still parse (backward compat).
    """
    if isinstance(item, dict):
        item_type = item.get("type")
        if item_type == "done":
            return ("done", None)
        if item_type == "chunk":
            chunk = item.get("chunk")
            return ("chunk", chunk) if isinstance(chunk, dict) else ("ignore", None)
        if "choices" in item or "usage" in item:
            return ("chunk", item)
        return ("ignore", None)
    if isinstance(item, str):
        text = item.strip()
        if text.startswith("data:"):
            text = text[5:].strip()
        if text == "[DONE]":
            return ("done", None)
        try:
            return ("chunk", json.loads(text))
        except json.JSONDecodeError:
            return ("ignore", None)
    return ("ignore", None)


def convert_openai_to_claude_response(
    openai_response: dict, original_request: ClaudeMessagesRequest
) -> dict:
    """Convert OpenAI response to Claude format."""

    # Extract response data
    choices = openai_response.get("choices", [])
    if not choices:
        raise HTTPException(status_code=500, detail="No choices in OpenAI response")

    choice = choices[0]
    message = choice.get("message", {})

    # Build Claude content blocks
    content_blocks = []

    # Add text content
    text_content = message.get("content")
    if text_content is not None:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": text_content})

    # Add tool calls
    tool_calls = message.get("tool_calls", []) or []
    for tool_call in tool_calls:
        if tool_call.get("type") == Constants.TOOL_FUNCTION:
            function_data = tool_call.get(Constants.TOOL_FUNCTION, {})
            try:
                arguments = json.loads(function_data.get("arguments", "{}"))
            except json.JSONDecodeError:
                arguments = {"raw_arguments": function_data.get("arguments", "")}

            content_blocks.append(
                {
                    "type": Constants.CONTENT_TOOL_USE,
                    "id": tool_call.get("id", f"tool_{uuid.uuid4()}"),
                    # Upstream echoes our alias: restore the Claude-side name.
                    "name": from_upstream_name(function_data.get("name", "")),
                    "input": arguments,
                }
            )

    # Ensure at least one content block
    if not content_blocks:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": ""})

    # Map finish reason
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = {
        "stop": Constants.STOP_END_TURN,
        "length": Constants.STOP_MAX_TOKENS,
        "tool_calls": Constants.STOP_TOOL_USE,
        "function_call": Constants.STOP_TOOL_USE,
    }.get(finish_reason, Constants.STOP_END_TURN)

    # Build Claude response
    claude_response = {
        "id": openai_response.get("id", f"msg_{uuid.uuid4()}"),
        "type": "message",
        "role": Constants.ROLE_ASSISTANT,
        "model": original_request.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": openai_response.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": openai_response.get("usage", {}).get(
                "completion_tokens", 0
            ),
        },
    }

    return claude_response


async def convert_openai_streaming_to_claude(
    openai_stream, original_request: ClaudeMessagesRequest, logger
):
    """Convert OpenAI streaming response to Claude streaming format."""

    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    # Send initial SSE events
    yield f"event: {Constants.EVENT_MESSAGE_START}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_START, 'message': {'id': message_id, 'type': 'message', 'role': Constants.ROLE_ASSISTANT, 'model': original_request.model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': 0, 'content_block': {'type': Constants.CONTENT_TEXT, 'text': ''}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_PING}\ndata: {json.dumps({'type': Constants.EVENT_PING}, ensure_ascii=False)}\n\n"

    # Process streaming chunks
    text_block_index = 0
    tool_block_counter = 0
    current_tool_calls = {}
    final_stop_reason = Constants.STOP_END_TURN

    try:
        async for item in openai_stream:
            kind, chunk = _parse_chat_item(item)
            if kind == "done":
                break
            if kind != "chunk":
                continue
            choices = chunk.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason")

            # Handle text delta
            if delta and "content" in delta and delta["content"] is not None:
                yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': text_block_index, 'delta': {'type': Constants.DELTA_TEXT, 'text': delta['content']}}, ensure_ascii=False)}\n\n"

            # Handle tool call deltas with improved incremental processing
            if "tool_calls" in delta and delta["tool_calls"]:
                for tc_delta in delta["tool_calls"]:
                    tc_index = tc_delta.get("index", 0)

                    # Initialize tool call tracking by index if not exists
                    if tc_index not in current_tool_calls:
                        current_tool_calls[tc_index] = {
                            "id": None,
                            "name": None,
                            "args_buffer": "",
                            "sent_len": 0,
                            "claude_index": None,
                            "started": False
                        }

                    tool_call = current_tool_calls[tc_index]

                    # Update tool call ID if provided
                    if tc_delta.get("id"):
                        tool_call["id"] = tc_delta["id"]

                    # Update function name and start content block if we have both id and name
                    function_data = tc_delta.get(Constants.TOOL_FUNCTION) or {}
                    if function_data.get("name"):
                        # Upstream echoes our alias: restore the Claude-side name.
                        tool_call["name"] = from_upstream_name(function_data["name"])

                    # Start content block when we have complete initial data
                    if (tool_call["id"] and tool_call["name"] and not tool_call["started"]):
                        tool_block_counter += 1
                        claude_index = text_block_index + tool_block_counter
                        tool_call["claude_index"] = claude_index
                        tool_call["started"] = True

                        yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': claude_index, 'content_block': {'type': Constants.CONTENT_TOOL_USE, 'id': tool_call['id'], 'name': tool_call['name'], 'input': {}}}, ensure_ascii=False)}\n\n"
                        # Flush any arguments that arrived before the block
                        # started, so the backstop accounting stays exact.
                        prefix = tool_call["args_buffer"]
                        if prefix:
                            yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': claude_index, 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': prefix}}, ensure_ascii=False)}\n\n"
                            tool_call["sent_len"] = len(prefix)

                    # Handle function arguments incrementally: forward each
                    # fragment as it arrives (Anthropic concatenates
                    # partial_json deltas); unsent tail flushed at the end.
                    if "arguments" in function_data and function_data["arguments"]:
                        fragment = function_data["arguments"]
                        tool_call["args_buffer"] += fragment
                        if tool_call["started"]:
                            yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_call['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': fragment}}, ensure_ascii=False)}\n\n"
                            tool_call["sent_len"] = len(tool_call["args_buffer"])

            # Handle finish reason
            if finish_reason:
                if finish_reason == "length":
                    final_stop_reason = Constants.STOP_MAX_TOKENS
                elif finish_reason in ["tool_calls", "function_call"]:
                    final_stop_reason = Constants.STOP_TOOL_USE
                elif finish_reason == "stop":
                    final_stop_reason = Constants.STOP_END_TURN
                else:
                    final_stop_reason = Constants.STOP_END_TURN
                break

    except Exception as e:
        # Handle any streaming errors gracefully
        logger.error(f"Streaming error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        return

    # Send final SSE events
    stats.add_tokens(usage_data)
    # Backstop: flush any tool-args tail that arrived before its block
    # started (or after the last incremental forward).
    for tool_data in current_tool_calls.values():
        if tool_data.get("started") and tool_data.get("claude_index") is not None:
            tail = tool_data.get("args_buffer", "")[tool_data.get("sent_len", 0):]
            if tail:
                yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_data['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': tail}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': text_block_index}, ensure_ascii=False)}\n\n"

    for tool_data in current_tool_calls.values():
        if tool_data.get("started") and tool_data.get("claude_index") is not None:
            yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': tool_data['claude_index']}, ensure_ascii=False)}\n\n"

    usage_data = {"input_tokens": 0, "output_tokens": 0}
    yield f"event: {Constants.EVENT_MESSAGE_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_DELTA, 'delta': {'stop_reason': final_stop_reason, 'stop_sequence': None}, 'usage': usage_data}, ensure_ascii=False)}\n\n"
    yield f"event: {Constants.EVENT_MESSAGE_STOP}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_STOP}, ensure_ascii=False)}\n\n"


async def convert_openai_streaming_to_claude_with_cancellation(
    openai_stream,
    original_request: ClaudeMessagesRequest,
    logger,
    http_request: Request,
    openai_client,
    request_id: str,
):
    """Convert OpenAI streaming response to Claude streaming format with cancellation support."""

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    _stream_t0 = time.monotonic()
    _first_token_at = None
    _prev_token_at = None
    stats.record_stream()

    # Send initial SSE events
    yield f"event: {Constants.EVENT_MESSAGE_START}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_START, 'message': {'id': message_id, 'type': 'message', 'role': Constants.ROLE_ASSISTANT, 'model': original_request.model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 0, 'output_tokens': 0}}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': 0, 'content_block': {'type': Constants.CONTENT_TEXT, 'text': ''}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_PING}\ndata: {json.dumps({'type': Constants.EVENT_PING}, ensure_ascii=False)}\n\n"

    # Process streaming chunks
    text_block_index = 0
    tool_block_counter = 0
    current_tool_calls = {}
    final_stop_reason = Constants.STOP_END_TURN
    usage_data = {"input_tokens": 0, "output_tokens": 0}

    try:
        async for item in openai_stream:
            # Check if client disconnected
            if await http_request.is_disconnected():
                logger.info(f"Client disconnected, cancelling request {request_id}")
                openai_client.cancel_request(request_id)
                break

            kind, chunk = _parse_chat_item(item)
            if kind == "done":
                break
            if kind != "chunk":
                continue
            # logger.info(f"OpenAI chunk: {chunk}")
            usage = chunk.get("usage", None)
            if usage:
                cache_read_input_tokens = 0
                prompt_tokens_details = usage.get('prompt_tokens_details', {})
                if prompt_tokens_details:
                    cache_read_input_tokens = prompt_tokens_details.get('cached_tokens', 0)
                usage_data = {
                    'input_tokens': usage.get('prompt_tokens', 0),
                    'output_tokens': usage.get('completion_tokens', 0),
                    'cache_read_input_tokens': cache_read_input_tokens
                }
            choices = chunk.get("choices", [])
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason")

            # Handle text delta
            if delta and "content" in delta and delta["content"] is not None:
                _now = time.monotonic()
                if _first_token_at is None:
                    _first_token_at = _now
                    stats.record_ttft(_now - _stream_t0)
                    logger.debug(
                        "Stream %s TTFT client=%.1fms",
                        request_id,
                        (_now - _stream_t0) * 1000,
                    )
                elif _prev_token_at is not None:
                    stats.record_itl(_now - _prev_token_at)
                _prev_token_at = _now
                yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': text_block_index, 'delta': {'type': Constants.DELTA_TEXT, 'text': delta['content']}}, ensure_ascii=False)}\n\n"

            # Handle tool call deltas with improved incremental processing
            if "tool_calls" in delta and delta["tool_calls"]:
                for tc_delta in delta["tool_calls"]:
                    tc_index = tc_delta.get("index", 0)

                    # Initialize tool call tracking by index if not exists
                    if tc_index not in current_tool_calls:
                        current_tool_calls[tc_index] = {
                            "id": None,
                            "name": None,
                            "args_buffer": "",
                            "sent_len": 0,
                            "claude_index": None,
                            "started": False
                        }

                    tool_call = current_tool_calls[tc_index]

                    # Update tool call ID if provided
                    if tc_delta.get("id"):
                        tool_call["id"] = tc_delta["id"]

                    # Update function name and start content block if we have both id and name
                    function_data = tc_delta.get(Constants.TOOL_FUNCTION) or {}
                    if function_data.get("name"):
                        # Upstream echoes our alias: restore the Claude-side name.
                        tool_call["name"] = from_upstream_name(function_data["name"])

                    # Start content block when we have complete initial data
                    if (tool_call["id"] and tool_call["name"] and not tool_call["started"]):
                        tool_block_counter += 1
                        claude_index = text_block_index + tool_block_counter
                        tool_call["claude_index"] = claude_index
                        tool_call["started"] = True

                        if _first_token_at is None:
                            _first_token_at = time.monotonic()
                            stats.record_ttft(_first_token_at - _stream_t0)
                        _prev_token_at = time.monotonic()

                        yield f"event: {Constants.EVENT_CONTENT_BLOCK_START}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_START, 'index': claude_index, 'content_block': {'type': Constants.CONTENT_TOOL_USE, 'id': tool_call['id'], 'name': tool_call['name'], 'input': {}}}, ensure_ascii=False)}\n\n"
                        # Flush any arguments that arrived before the block
                        # started, so the backstop accounting stays exact.
                        prefix = tool_call["args_buffer"]
                        if prefix:
                            yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': claude_index, 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': prefix}}, ensure_ascii=False)}\n\n"
                            tool_call["sent_len"] = len(prefix)

                    # Handle function arguments incrementally: forward each
                    # fragment as it arrives (Anthropic concatenates
                    # partial_json deltas); unsent tail flushed at the end.
                    if "arguments" in function_data and function_data["arguments"]:
                        fragment = function_data["arguments"]
                        tool_call["args_buffer"] += fragment
                        if tool_call["started"]:
                            yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_call['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': fragment}}, ensure_ascii=False)}\n\n"
                            tool_call["sent_len"] = len(tool_call["args_buffer"])

            # Handle finish reason
            if finish_reason:
                if finish_reason == "length":
                    final_stop_reason = Constants.STOP_MAX_TOKENS
                elif finish_reason in ["tool_calls", "function_call"]:
                    final_stop_reason = Constants.STOP_TOOL_USE
                elif finish_reason == "stop":
                    final_stop_reason = Constants.STOP_END_TURN
                else:
                    final_stop_reason = Constants.STOP_END_TURN

    except HTTPException as e:
        # Handle cancellation
        if e.status_code == 499:
            logger.info(f"Request {request_id} was cancelled")
            error_event = {
                "type": "error",
                "error": {
                    "type": "cancelled",
                    "message": "Request was cancelled by client",
                },
            }
            yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            return
        else:
            # Headers already went out: map to an SSE error event (same
            # contract as the responses path) instead of truncating.
            logger.error(f"Stream {request_id} upstream {e.status_code}: {e.detail}")
            stats.record_stream_error(e.status_code)
            error_event = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": openai_client.classify_openai_error(e.detail),
                },
            }
            yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            return
    except Exception as e:
        # Handle any streaming errors gracefully
        logger.error(f"Streaming error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        stats.record_stream_error(500)
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
        }
        yield f"event: error\ndata: {json.dumps(error_event, ensure_ascii=False)}\n\n"
        return

    # Send final SSE events
    # Chat streams only count hits in endpoints.py — feed the final usage
    # into stats here so streamed tokens aren't invisible.
    stats.add_tokens(usage_data)
    # Backstop: flush any tool-args tail that arrived before its block
    # started (or after the last incremental forward).
    for tool_data in current_tool_calls.values():
        if tool_data.get("started") and tool_data.get("claude_index") is not None:
            tail = tool_data.get("args_buffer", "")[tool_data.get("sent_len", 0):]
            if tail:
                yield f"event: {Constants.EVENT_CONTENT_BLOCK_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_DELTA, 'index': tool_data['claude_index'], 'delta': {'type': Constants.DELTA_INPUT_JSON, 'partial_json': tail}}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': text_block_index}, ensure_ascii=False)}\n\n"

    for tool_data in current_tool_calls.values():
        if tool_data.get("started") and tool_data.get("claude_index") is not None:
            yield f"event: {Constants.EVENT_CONTENT_BLOCK_STOP}\ndata: {json.dumps({'type': Constants.EVENT_CONTENT_BLOCK_STOP, 'index': tool_data['claude_index']}, ensure_ascii=False)}\n\n"

    yield f"event: {Constants.EVENT_MESSAGE_DELTA}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_DELTA, 'delta': {'stop_reason': final_stop_reason, 'stop_sequence': None}, 'usage': usage_data}, ensure_ascii=False)}\n\n"
    yield f"event: {Constants.EVENT_MESSAGE_STOP}\ndata: {json.dumps({'type': Constants.EVENT_MESSAGE_STOP}, ensure_ascii=False)}\n\n"
