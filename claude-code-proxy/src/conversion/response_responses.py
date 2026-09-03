"""Convert OpenAI Responses API results back to Claude Messages API format."""

import asyncio
import json
import uuid
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.core.config import config
from src.core.constants import Constants
from src.models.claude import ClaudeMessagesRequest


def convert_responses_to_claude_response(
    responses_response: dict, original_request: ClaudeMessagesRequest
) -> dict:
    """Convert a completed Responses object to Claude message format."""
    output = responses_response.get("output", []) or []

    content_blocks: List[Dict[str, Any]] = []
    has_function_call = False
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            for part in item.get("content", []) or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text":
                    content_blocks.append(
                        {"type": Constants.CONTENT_TEXT, "text": part.get("text", "")}
                    )
                elif part.get("type") == "refusal":
                    content_blocks.append(
                        {
                            "type": Constants.CONTENT_TEXT,
                            "text": part.get("refusal", ""),
                        }
                    )
        elif itype == "function_call":
            has_function_call = True
            content_blocks.append(
                {
                    "type": Constants.CONTENT_TOOL_USE,
                    "id": item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": item.get("name", ""),
                    "input": _parse_arguments(item.get("arguments")),
                }
            )
        # "reasoning" items carry opaque encrypted_content: not translatable
        # to Claude thinking blocks, so they are intentionally skipped.

    if not content_blocks:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": ""})

    stop_reason = _map_status_to_stop_reason(responses_response, has_function_call)

    usage = responses_response.get("usage", {}) or {}
    claude_response = {
        "id": responses_response.get("id", f"msg_{uuid.uuid4().hex[:24]}"),
        "type": "message",
        "role": Constants.ROLE_ASSISTANT,
        "model": original_request.model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }
    return claude_response


def _parse_arguments(arguments) -> Dict[str, Any]:
    if not arguments:
        return {}
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments)
        return parsed if isinstance(parsed, dict) else {"raw_arguments": arguments}
    except (json.JSONDecodeError, TypeError):
        return {"raw_arguments": str(arguments)}


def _map_status_to_stop_reason(responses_response: dict, has_function_call: bool) -> str:
    if has_function_call:
        return Constants.STOP_TOOL_USE
    status = responses_response.get("status")
    if status == "incomplete":
        reason = (responses_response.get("incomplete_details") or {}).get("reason", "")
        if reason == "max_output_tokens":
            return Constants.STOP_MAX_TOKENS
        return Constants.STOP_ERROR
    if status == "failed":
        return Constants.STOP_ERROR
    return Constants.STOP_END_TURN


async def convert_responses_streaming_to_claude_with_cancellation(
    responses_stream,
    original_request: ClaudeMessagesRequest,
    logger,
    http_request: Request,
    responses_client,
    request_id: str,
):
    """Convert a Responses SSE stream to Claude SSE format with cancellation."""

    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    yield _sse(
        Constants.EVENT_MESSAGE_START,
        {
            "type": Constants.EVENT_MESSAGE_START,
            "message": {
                "id": message_id,
                "type": "message",
                "role": Constants.ROLE_ASSISTANT,
                "model": original_request.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    yield _sse(
        Constants.EVENT_CONTENT_BLOCK_START,
        {
            "type": Constants.EVENT_CONTENT_BLOCK_START,
            "index": 0,
            "content_block": {"type": Constants.CONTENT_TEXT, "text": ""},
        },
    )
    yield _sse(Constants.EVENT_PING, {"type": Constants.EVENT_PING})

    text_block_index = 0
    tool_block_counter = 0
    # item_id -> {"claude_index", "id", "name", "args_buffer", "started", "done_sent"}
    function_calls: Dict[str, Dict[str, Any]] = {}
    has_function_call = False
    usage_data = {"input_tokens": 0, "output_tokens": 0}
    final_status: str = "completed"
    incomplete_reason: str = ""

    # Pump upstream events into a queue so we can inject SSE keepalive pings
    # during long silent stretches (reasoning phases). Without bytes on the
    # wire, Claude Code shows "Waiting for API response" after ~20s and its
    # idle watchdogs eventually abort + retry the stream.
    queue: "asyncio.Queue" = asyncio.Queue()
    upstream_done = asyncio.Event()

    async def _pump():
        try:
            async for raw_event in responses_stream:
                await queue.put(("event", raw_event))
        except Exception as e:
            await queue.put(("error", e))
        finally:
            upstream_done.set()
            await queue.put(("end", None))

    async def _keepalive():
        interval = config.stream_keepalive_secs
        if interval <= 0:
            return
        try:
            while not upstream_done.is_set():
                await asyncio.sleep(interval)
                if not upstream_done.is_set():
                    await queue.put(("ping", None))
        except asyncio.CancelledError:
            pass

    pump_task = asyncio.create_task(_pump())
    keepalive_task = asyncio.create_task(_keepalive())

    try:
        while True:
            if await http_request.is_disconnected():
                logger.info(f"Client disconnected, cancelling request {request_id}")
                responses_client.cancel_request(request_id)
                break

            kind, item = await queue.get()
            if kind == "end":
                break
            if kind == "ping":
                yield _sse(Constants.EVENT_PING, {"type": Constants.EVENT_PING})
                continue
            if kind == "error":
                raise item

            event_type, payload = _split_event(item)
            if payload is None:
                continue

            if event_type == "response.output_text.delta":
                delta = payload.get("delta")
                if delta:
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_DELTA,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                            "index": text_block_index,
                            "delta": {"type": Constants.DELTA_TEXT, "text": delta},
                        },
                    )
            elif event_type == "response.output_item.added":
                item = payload.get("item", {}) or {}
                if item.get("type") == "function_call":
                    item_id = (
                        item.get("id")
                        or item.get("call_id")
                        or f"fc_{uuid.uuid4().hex[:12]}"
                    )
                    tool_block_counter += 1
                    claude_index = text_block_index + tool_block_counter
                    function_calls[item_id] = {
                        "claude_index": claude_index,
                        "id": item.get("call_id") or item.get("id") or item_id,
                        "name": item.get("name", ""),
                        "args_buffer": item.get("arguments") or "",
                        "started": True,
                        "done_sent": False,
                    }
                    has_function_call = True
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_START,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_START,
                            "index": claude_index,
                            "content_block": {
                                "type": Constants.CONTENT_TOOL_USE,
                                "id": function_calls[item_id]["id"],
                                "name": function_calls[item_id]["name"],
                                "input": {},
                            },
                        },
                    )
            elif event_type == "response.function_call_arguments.delta":
                item_id = payload.get("item_id", "")
                entry = function_calls.get(item_id)
                if entry is not None and entry["started"]:
                    entry["args_buffer"] += payload.get("delta", "")
            elif event_type in (
                "response.function_call_arguments.done",
                "response.output_item.done",
            ):
                item = payload.get("item") if event_type == "response.output_item.done" else None
                if event_type == "response.output_item.done":
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    item_id = item.get("id") or item.get("call_id", "")
                    entry = function_calls.get(item_id)
                    if entry is None:
                        continue
                    if item.get("arguments"):
                        entry["args_buffer"] = item["arguments"]
                    if item.get("name"):
                        entry["name"] = item["name"]
                else:
                    item_id = payload.get("item_id", "")
                    entry = function_calls.get(item_id)
                    if entry is None:
                        continue
                    if payload.get("arguments"):
                        entry["args_buffer"] = payload["arguments"]
                if not entry["done_sent"]:
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_DELTA,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                            "index": entry["claude_index"],
                            "delta": {
                                "type": Constants.DELTA_INPUT_JSON,
                                "partial_json": entry["args_buffer"],
                            },
                        },
                    )
                    entry["done_sent"] = True
            elif event_type == "response.completed":
                response = payload.get("response", {}) or {}
                final_status = response.get("status", "completed")
                incomplete_reason = (
                    response.get("incomplete_details") or {}
                ).get("reason", "")
                usage = response.get("usage", {}) or {}
                usage_data = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }
                for item in response.get("output", []) or []:
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        has_function_call = True
            elif event_type == "response.failed":
                final_status = "failed"
            # All other events (created/in_progress/reasoning/content_part
            # markers/ping) carry no Claude-visible payload and are skipped.

    except HTTPException as e:
        if e.status_code == 499:
            logger.info(f"Request {request_id} was cancelled")
            yield _sse(
                "error",
                {
                    "type": "error",
                    "error": {"type": "cancelled", "message": "Request was cancelled by client"},
                },
            )
            return
        raise
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        yield _sse(
            "error",
            {
                "type": "error",
                "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
            },
        )
        return

    # Stop background tasks before emitting the closing events.
    upstream_done.set()
    for task in (pump_task, keepalive_task):
        if not task.done():
            task.cancel()

    # Flush any function call that never got an explicit done event.
    for entry in function_calls.values():
        if entry.get("started") and not entry.get("done_sent"):
            yield _sse(
                Constants.EVENT_CONTENT_BLOCK_DELTA,
                {
                    "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                    "index": entry["claude_index"],
                    "delta": {
                        "type": Constants.DELTA_INPUT_JSON,
                        "partial_json": entry.get("args_buffer", ""),
                    },
                },
            )
            entry["done_sent"] = True

    if has_function_call:
        final_stop_reason = Constants.STOP_TOOL_USE
    elif final_status == "incomplete":
        final_stop_reason = (
            Constants.STOP_MAX_TOKENS
            if incomplete_reason == "max_output_tokens"
            else Constants.STOP_ERROR
        )
    elif final_status == "failed":
        final_stop_reason = Constants.STOP_ERROR
    else:
        final_stop_reason = Constants.STOP_END_TURN

    yield _sse(
        Constants.EVENT_CONTENT_BLOCK_STOP,
        {"type": Constants.EVENT_CONTENT_BLOCK_STOP, "index": text_block_index},
    )
    for entry in function_calls.values():
        if entry.get("started"):
            yield _sse(
                Constants.EVENT_CONTENT_BLOCK_STOP,
                {
                    "type": Constants.EVENT_CONTENT_BLOCK_STOP,
                    "index": entry["claude_index"],
                },
            )
    yield _sse(
        Constants.EVENT_MESSAGE_DELTA,
        {
            "type": Constants.EVENT_MESSAGE_DELTA,
            "delta": {"stop_reason": final_stop_reason, "stop_sequence": None},
            "usage": usage_data,
        },
    )
    yield _sse(Constants.EVENT_MESSAGE_STOP, {"type": Constants.EVENT_MESSAGE_STOP})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _split_event(raw_event: str):
    """Split 'event: X\\ndata: {...}' into (X, dict|None)."""
    event_type = "message"
    data_str = None
    for line in raw_event.split("\n"):
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            chunk = line[5:].strip()
            data_str = chunk if data_str is None else data_str + "\n" + chunk
    if not data_str or data_str == "[DONE]":
        return event_type, None
    try:
        return event_type, json.loads(data_str)
    except json.JSONDecodeError:
        return event_type, None
