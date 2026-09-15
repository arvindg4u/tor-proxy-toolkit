"""Convert OpenAI Responses API results back to Claude Messages API format."""

import asyncio
import json
import time
import uuid
from typing import Any, Dict, List

from fastapi import HTTPException, Request

from src.core.config import config
from src.core.constants import Constants
from src.conversion.tool_names import from_upstream_name
from src.core.stats import request_usage_key, stats
from src.models.claude import ClaudeMessagesRequest


def convert_responses_to_claude_response(
    responses_response: dict, original_request: ClaudeMessagesRequest
) -> dict:
    """Convert a completed Responses object to Claude message format."""
    output = responses_response.get("output", []) or []

    content_blocks: List[Dict[str, Any]] = []
    has_function_call = False
    thinking_texts: List[str] = []
    thinking_cfg = getattr(original_request, "thinking", None)
    want_thinking = bool(
        thinking_cfg is not None and thinking_cfg.type == "enabled"
    )
    for item in output:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "reasoning":
            if want_thinking:
                for part in item.get("summary", []) or []:
                    if isinstance(part, dict) and part.get("type") == "summary_text":
                        text = part.get("text", "")
                        if text:
                            thinking_texts.append(text)
            continue
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
                    # Upstream echoes our alias: restore the Claude-side name.
                    "name": from_upstream_name(item.get("name", "")),
                    "input": _parse_arguments(item.get("arguments")),
                }
            )
        # "reasoning" items carry opaque encrypted_content: not translatable
        # to Claude thinking blocks, so they are intentionally skipped
        # (summaries collected above).

    if thinking_texts:
        content_blocks.insert(
            0,
            {
                "type": Constants.CONTENT_THINKING,
                "thinking": "\n\n".join(thinking_texts),
                "signature": "",
            },
        )

    if not content_blocks:
        content_blocks.append({"type": Constants.CONTENT_TEXT, "text": ""})

    stop_reason = _map_status_to_stop_reason(responses_response, has_function_call)

    usage = responses_response.get("usage", {}) or {}
    # Snapshot for the next turn's message_start base (non-streaming turns
    # count here too, so a non-streamed turn doesn't leave a stale base).
    stats.note_response_usage(
        {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": (
                usage.get("input_tokens_details") or {}
            ).get("cached_tokens", 0),
            "cache_creation_input_tokens": 0,
        },
        key=request_usage_key(original_request),
    )
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
            # Upstream prompt cache hits (implicit prefix-match caching):
            # surface them so Claude Code sees real cache performance
            # instead of assuming zero caching.
            "cache_read_input_tokens": (usage.get("input_tokens_details") or {}).get(
                "cached_tokens", 0
            ),
            "cache_creation_input_tokens": 0,
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
    """Convert a Responses SSE stream to Claude SSE format with cancellation.

    Phase 2 optimistic start: the initial ``message_start`` /
    ``content_block_start`` / ``ping`` burst is yielded before the upstream
    POST completes, so response headers reach the client in milliseconds
    regardless of upstream TTFT. The upstream generator is consumed lazily
    inside the pump task; an upstream rejection after headers goes out is
    therefore mapped to an SSE ``error`` event (the failure dump for
    watch-429 has already been written by the client).
    """

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    _stream_t0 = time.monotonic()
    _first_token_at: float | None = None
    _prev_token_at: float | None = None
    stats.record_stream()
    # Per-conversation usage base: concurrent sessions each keep their own.
    usage_key = request_usage_key(original_request)

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
                # Last turn's totals: keeps the live context counter
                # continuous instead of collapsing to zero each turn.
                "usage": stats.last_response_usage(usage_key),
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
    # SSE comment ping alongside the event ping: spec-ignored by clients,
    # but edge proxies / LBs only reset idle timers on bytes.
    yield ": ping\n\n"

    text_block_index = 0
    tool_block_counter = 0
    # item_id -> {"claude_index", "id", "name", "args_buffer", "sent_len", "started", "done_sent"}
    function_calls: Dict[str, Dict[str, Any]] = {}
    # Arg fragments for ids not yet seen in output_item.added (out-of-order
    # upstream); adopted when the added event arrives, dropped at the end.
    pending_args: Dict[str, str] = {}
    has_function_call = False
    # Thinking display: when the Claude request enabled thinking, upstream
    # reasoning summaries stream as a Claude thinking block so long
    # reasoning phases are visible (and count live) instead of arriving as
    # one silent gap. Opt-in upstream via reasoning.summary="auto"
    # (request_responses.py); without it the upstream sends no summaries.
    thinking = getattr(original_request, "thinking", None)
    want_thinking = bool(thinking is not None and thinking.type == "enabled")
    thinking_index: int | None = None
    thinking_open = False
    usage_data = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    final_status: str = "completed"
    incomplete_reason: str = ""

    # Pump upstream events into a queue so we can inject SSE keepalive pings
    # during long silent stretches (reasoning phases). Without bytes on the
    # wire, Claude Code shows "Waiting for API response" after ~20s and its
    # idle watchdogs eventually abort + retry the stream.
    # Bounded queue applies backpressure: a fast upstream pauses instead of
    # buffering unboundedly when the downstream client is slow.
    queue: "asyncio.Queue" = asyncio.Queue(maxsize=100)
    upstream_done = asyncio.Event()
    keepalive_interval = config.stream_keepalive_secs
    # Disconnect polling stays finite even when ping emission is disabled.
    _poll_interval = keepalive_interval if keepalive_interval > 0 else 15.0

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
        if keepalive_interval <= 0:
            return
        try:
            while not upstream_done.is_set():
                await asyncio.sleep(keepalive_interval)
                if not upstream_done.is_set():
                    await queue.put(("ping", None))
        except asyncio.CancelledError:
            pass

    pump_task = asyncio.create_task(_pump())
    keepalive_task = asyncio.create_task(_keepalive())
    _stream_failed = False

    try:
        while True:
            if await http_request.is_disconnected():
                logger.info(f"Client disconnected, cancelling request {request_id}")
                responses_client.cancel_request(request_id)
                break

            try:
                # Silence itself triggers the wait so disconnects are
                # detected even when neither upstream nor keepalive yields.
                # (When keepalives are disabled the poll interval still
                # applies so disconnects don't go unnoticed in silence.)
                kind, item = await asyncio.wait_for(
                    queue.get(), timeout=_poll_interval
                )
            except asyncio.TimeoutError:
                if await http_request.is_disconnected():
                    logger.info(
                        f"Client disconnected during silence, cancelling {request_id}"
                    )
                    responses_client.cancel_request(request_id)
                    break
                continue
            if kind == "end":
                break
            if kind == "ping":
                stats.record_keepalive()
                yield _sse(Constants.EVENT_PING, {"type": Constants.EVENT_PING})
                yield ": ping\n\n"
                continue
            if kind == "error":
                raise item

            event_type, payload = _split_event(item)
            if payload is None:
                continue

            if event_type == "response.output_text.delta":
                delta = payload.get("delta")
                if delta:
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
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_DELTA,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                            "index": text_block_index,
                            "delta": {"type": Constants.DELTA_TEXT, "text": delta},
                        },
                    )
            elif event_type == "response.reasoning_summary_part.added":
                if want_thinking and not thinking_open:
                    tool_block_counter += 1
                    thinking_index = text_block_index + tool_block_counter
                    thinking_open = True
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_START,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_START,
                            "index": thinking_index,
                            "content_block": {
                                "type": Constants.CONTENT_THINKING,
                                "thinking": "",
                                "signature": "",
                            },
                        },
                    )
            elif event_type == "response.reasoning_summary_text.delta":
                delta = payload.get("delta")
                if want_thinking and delta:
                    if not thinking_open:
                        # Defensive: some upstreams skip part.added.
                        tool_block_counter += 1
                        thinking_index = text_block_index + tool_block_counter
                        thinking_open = True
                        yield _sse(
                            Constants.EVENT_CONTENT_BLOCK_START,
                            {
                                "type": Constants.EVENT_CONTENT_BLOCK_START,
                                "index": thinking_index,
                                "content_block": {
                                    "type": Constants.CONTENT_THINKING,
                                    "thinking": "",
                                    "signature": "",
                                },
                            },
                        )
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_DELTA,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                            "index": thinking_index,
                            "delta": {
                                "type": Constants.DELTA_THINKING,
                                "thinking": delta,
                            },
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
                        # Upstream echoes our alias: restore the Claude-side name.
                        "name": from_upstream_name(item.get("name", "")),
                        "args_buffer": "",
                        "sent_len": 0,
                        "started": True,
                        "done_sent": False,
                    }
                    # Seed with any delta fragments that arrived before the
                    # added event (out-of-order upstream); they flush below.
                    seed = pending_args.pop(item.get("id"), "") + pending_args.pop(
                        item.get("call_id"), ""
                    )
                    if not item.get("arguments") and seed:
                        function_calls[item_id]["args_buffer"] = seed
                    elif item.get("arguments"):
                        function_calls[item_id]["args_buffer"] = item.get("arguments")
                    has_function_call = True
                    if _first_token_at is None:
                        _first_token_at = time.monotonic()
                        stats.record_ttft(_first_token_at - _stream_t0)
                    _prev_token_at = time.monotonic()
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
                    # Phase 3: forward any arguments snapshot incrementally
                    # instead of waiting for the done event.
                    initial_args = function_calls[item_id]["args_buffer"]
                    if initial_args:
                        yield _sse(
                            Constants.EVENT_CONTENT_BLOCK_DELTA,
                            {
                                "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                                "index": claude_index,
                                "delta": {
                                    "type": Constants.DELTA_INPUT_JSON,
                                    "partial_json": initial_args,
                                },
                            },
                        )
                        function_calls[item_id]["sent_len"] = len(initial_args)
            elif event_type == "response.function_call_arguments.delta":
                item_id = payload.get("item_id", "")
                entry = function_calls.get(item_id)
                fragment = payload.get("delta", "")
                if not fragment:
                    continue
                if entry is None:
                    # Added event hasn't arrived (or never will): buffer for
                    # adoption at added-time instead of dropping the call.
                    pending_args[item_id] = pending_args.get(item_id, "") + fragment
                    continue
                # Phase 3: stream each fragment as it arrives
                # (Anthropic concatenates partial_json deltas).
                entry["args_buffer"] += fragment
                if entry["started"]:
                    yield _sse(
                        Constants.EVENT_CONTENT_BLOCK_DELTA,
                        {
                            "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                            "index": entry["claude_index"],
                            "delta": {
                                "type": Constants.DELTA_INPUT_JSON,
                                "partial_json": fragment,
                            },
                        },
                    )
                    entry["sent_len"] = len(entry["args_buffer"])
            elif event_type in (
                "response.function_call_arguments.done",
                "response.output_item.done",
            ):
                item = payload.get("item") if event_type == "response.output_item.done" else None
                if event_type == "response.output_item.done":
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        if thinking_open:
                            yield _sse(
                                Constants.EVENT_CONTENT_BLOCK_DELTA,
                                {
                                    "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                                    "index": thinking_index,
                                    "delta": {
                                        "type": Constants.DELTA_SIGNATURE,
                                        "signature": "",
                                    },
                                },
                            )
                            yield _sse(
                                Constants.EVENT_CONTENT_BLOCK_STOP,
                                {
                                    "type": Constants.EVENT_CONTENT_BLOCK_STOP,
                                    "index": thinking_index,
                                },
                            )
                            thinking_open = False
                        continue
                    if not isinstance(item, dict) or item.get("type") != "function_call":
                        continue
                    item_id = item.get("id") or item.get("call_id", "")
                    entry = function_calls.get(item_id)
                    if entry is None:
                        continue
                    if item.get("arguments") and len(item["arguments"]) > len(
                        entry["args_buffer"]
                    ):
                        entry["args_buffer"] = item["arguments"]
                    if item.get("name"):
                        entry["name"] = from_upstream_name(item["name"])
                else:
                    item_id = payload.get("item_id", "")
                    entry = function_calls.get(item_id)
                    if entry is None:
                        continue
                    if payload.get("arguments") and len(payload["arguments"]) > len(
                        entry["args_buffer"]
                    ):
                        entry["args_buffer"] = payload["arguments"]
                if not entry["done_sent"]:
                    # Phase 3 backstop: only the unsent tail (fragments
                    # already streamed incrementally above).
                    tail = entry["args_buffer"][entry.get("sent_len", 0):]
                    if tail:
                        yield _sse(
                            Constants.EVENT_CONTENT_BLOCK_DELTA,
                            {
                                "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                                "index": entry["claude_index"],
                                "delta": {
                                    "type": Constants.DELTA_INPUT_JSON,
                                    "partial_json": tail,
                                },
                            },
                        )
                        entry["sent_len"] = len(entry["args_buffer"])
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
                    "cache_read_input_tokens": (
                        usage.get("input_tokens_details") or {}
                    ).get("cached_tokens", 0),
                    "cache_creation_input_tokens": 0,
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
        else:
            # Phase 2: headers already went out (optimistic start), so the
            # status line is fixed at 200. Map the upstream rejection to an
            # SSE error event with the same classified message the pre-header
            # JSON path used. _dump_failure already recorded the failure for
            # watch-429 before this exception was raised.
            from src.core.responses_client import classify_responses_error

            logger.error(
                f"Stream {request_id} upstream {e.status_code} post-headers: {e.detail}"
            )
            stats.record_stream_error(e.status_code)
            yield _sse(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": classify_responses_error(e.detail),
                    },
                },
            )
        _stream_failed = True
    except Exception as e:
        logger.error(f"Streaming error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        stats.record_stream_error(500)
        yield _sse(
            "error",
            {
                "type": "error",
                "error": {"type": "api_error", "message": f"Streaming error: {str(e)}"},
            },
        )
        _stream_failed = True
    finally:
        # Always stop background tasks: the old early returns skipped the
        # cleanup below and leaked pump/keepalive (plus a queue.put blocked
        # on a full queue with no consumer) forever.
        upstream_done.set()
        for task in (pump_task, keepalive_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(pump_task, keepalive_task, return_exceptions=True)
        aclose = getattr(responses_stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass

    if _stream_failed:
        return

    # Streaming requests only count hits in endpoints.py — feed the final
    # usage (incl. cache hits) into stats here so tokens aren't invisible.
    # Also snapshots it as the next turn's message_start base.
    stats.add_tokens(usage_data, key=usage_key)

    # Flush any function call that never got an explicit done event
    # (only the unsent tail — fragments already streamed above).
    for entry in function_calls.values():
        if entry.get("started") and not entry.get("done_sent"):
            tail = entry.get("args_buffer", "")[entry.get("sent_len", 0):]
            if tail:
                yield _sse(
                    Constants.EVENT_CONTENT_BLOCK_DELTA,
                    {
                        "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                        "index": entry["claude_index"],
                        "delta": {
                            "type": Constants.DELTA_INPUT_JSON,
                            "partial_json": tail,
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
    if thinking_open:
        # Safety net: reasoning item done never arrived (e.g. upstream
        # only sent summary deltas). Never leave the block dangling.
        yield _sse(
            Constants.EVENT_CONTENT_BLOCK_DELTA,
            {
                "type": Constants.EVENT_CONTENT_BLOCK_DELTA,
                "index": thinking_index,
                "delta": {"type": Constants.DELTA_SIGNATURE, "signature": ""},
            },
        )
        yield _sse(
            Constants.EVENT_CONTENT_BLOCK_STOP,
            {"type": Constants.EVENT_CONTENT_BLOCK_STOP, "index": thinking_index},
        )
        thinking_open = False
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
