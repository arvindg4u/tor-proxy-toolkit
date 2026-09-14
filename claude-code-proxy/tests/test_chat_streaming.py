"""Phase 3 chat-path tests: dict protocol + incremental tool args.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_chat_streaming.py
(no third-party test deps required).
"""

import asyncio
import json
import logging
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from src.conversion.response_converter import (  # noqa: E402
    _parse_chat_item,
    convert_openai_streaming_to_claude_with_cancellation,
)
from src.core.stats import stats  # noqa: E402

logger = logging.getLogger("test")


class FakeRequest:
    async def is_disconnected(self):
        return False


class FakeClient:
    def __init__(self):
        self.cancelled = []

    def cancel_request(self, request_id):
        self.cancelled.append(request_id)
        return True

    @staticmethod
    def classify_openai_error(detail):
        return f"classified: {detail}"


class FakeOriginal:
    model = "test-model"


def _chunk(delta, finish_reason=None, usage=None):
    d = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    if usage is not None:
        d["usage"] = usage
    return {"type": "chunk", "chunk": d}


async def _collect(stream):
    out = []
    gen = convert_openai_streaming_to_claude_with_cancellation(
        stream, FakeOriginal(), logger, FakeRequest(), FakeClient(), "rid-chat"
    )
    async for item in gen:
        out.append(item)
    return "".join(out)


def _frames(blob):
    """Map event name -> list of data payloads."""
    frames = {}
    for raw in blob.split("\n\n"):
        raw = raw.strip()
        if not raw or raw.startswith(":"):
            continue
        etype, payload = None, None
        for line in raw.split("\n"):
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                payload = json.loads(line[5:].strip())
        if etype:
            frames.setdefault(etype, []).append(payload)
    return frames


def test_parse_chat_item_dict_protocol():
    kind, chunk = _parse_chat_item({"type": "chunk", "chunk": {"a": 1}})
    assert (kind, chunk) == ("chunk", {"a": 1})
    assert _parse_chat_item({"type": "done"}) == ("done", None)
    kind, chunk = _parse_chat_item({"choices": [], "usage": {}})
    assert kind == "chunk"


def test_parse_chat_item_legacy_strings():
    kind, chunk = _parse_chat_item('data: {"choices": []}')
    assert kind == "chunk" and chunk == {"choices": []}
    assert _parse_chat_item("data: [DONE]") == ("done", None)
    assert _parse_chat_item("[DONE]") == ("done", None)
    assert _parse_chat_item("data: not-json") == ("ignore", None)
    assert _parse_chat_item("") == ("ignore", None)
    assert _parse_chat_item(None) == ("ignore", None)


def test_dict_text_stream():
    async def run():
        async def stream():
            yield _chunk({"content": "hello"})
            yield _chunk({"content": " world"})
            yield _chunk({}, finish_reason="stop")

        frames = _frames(await _collect(stream()))
        deltas = frames.get("content_block_delta", [])
        texts = [
            d["delta"]["text"] for d in deltas if d["delta"].get("type") == "text_delta"
        ]
        assert texts == ["hello", " world"], texts
        assert "message_stop" in frames
        assert frames["message_delta"][0]["delta"]["stop_reason"] == "end_turn"

    asyncio.run(run())


def test_legacy_string_stream_still_works():
    async def run():
        async def stream():
            yield 'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": null}]}'
            yield "data: [DONE]"

        frames = _frames(await _collect(stream()))
        deltas = frames.get("content_block_delta", [])
        assert deltas and deltas[0]["delta"]["text"] == "hi"

    asyncio.run(run())


def test_incremental_tool_args():
    """Fragments stream as they arrive; concatenation reconstructs args."""
    full_args = '{"location": "Paris, France", "units": "metric"}'
    frags = [full_args[:10], full_args[10:25], full_args[25:]]

    async def run():
        async def stream():
            yield _chunk(
                {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "weather", "arguments": ""}}]}
            )
            for f in frags:
                yield _chunk(
                    {"tool_calls": [{"index": 0, "function": {"arguments": f}}]}
                )
            yield _chunk({}, finish_reason="tool_calls")

        frames = _frames(await _collect(stream()))
        deltas = [
            d["delta"]["partial_json"]
            for d in frames.get("content_block_delta", [])
            if d["delta"].get("type") == "input_json_delta"
        ]
        # One delta per fragment (incremental), concatenating to full args.
        assert deltas == frags, deltas
        assert "".join(deltas) == full_args
        assert json.loads("".join(deltas))["location"] == "Paris, France"
        assert frames["message_delta"][0]["delta"]["stop_reason"] == "tool_use"

    asyncio.run(run())


def test_post_header_error_becomes_sse_error():
    """Upstream failure after headers -> SSE error event, no raise."""

    async def run():
        async def stream():
            yield _chunk({"content": "hi"})
            raise HTTPException(status_code=429, detail="quota exploded")

        errors_before = stats.snapshot()["errors"]
        gen = convert_openai_streaming_to_claude_with_cancellation(
            stream(), FakeOriginal(), logger, FakeRequest(), FakeClient(), "rid-err"
        )
        out = "".join([item async for item in gen])
        frames = _frames(out)
        assert "error" in frames, frames.keys()
        assert "classified: quota exploded" in frames["error"][0]["error"]["message"]
        assert stats.snapshot()["errors"] == errors_before + 1

    asyncio.run(run())


def test_backstop_flushes_pre_start_args():
    """Args arriving before block start are flushed at the end, not lost."""

    async def run():
        async def stream():
            # Arguments fragment before id+name are known.
            yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"a":'}}]})
            yield _chunk(
                {"tool_calls": [{"index": 0, "id": "call_9", "function": {"name": "f", "arguments": ""}}]}
            )
            yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '1}'}}]})
            yield _chunk({}, finish_reason="tool_calls")

        frames = _frames(await _collect(stream()))
        deltas = [
            d["delta"]["partial_json"]
            for d in frames.get("content_block_delta", [])
            if d["delta"].get("type") == "input_json_delta"
        ]
        assert "".join(deltas) == '{"a":1}', deltas

    asyncio.run(run())


_TESTS = [
    test_parse_chat_item_dict_protocol,
    test_parse_chat_item_legacy_strings,
    test_dict_text_stream,
    test_legacy_string_stream_still_works,
    test_incremental_tool_args,
    test_backstop_flushes_pre_start_args,
    test_post_header_error_becomes_sse_error,
]


def main() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        else:
            print(f"ok {fn.__name__}")
    print(f"{len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
