"""Phase 2 liveness + Phase 3 bytes-framing tests.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_stream_liveness.py
(no third-party test deps required).
"""

import asyncio
import json
import os
import random
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from src.conversion.response_responses import (  # noqa: E402
    convert_responses_streaming_to_claude_with_cancellation,
    _split_event,
)
from src.core.responses_client import (  # noqa: E402
    _extract_sse_events,
    _parse_sse_event,
)


class FakeRequest:
    def __init__(self, disconnected=False):
        self._disconnected = disconnected

    async def is_disconnected(self):
        return self._disconnected


class FakeClient:
    def __init__(self):
        self.cancelled = []

    def cancel_request(self, request_id):
        self.cancelled.append(request_id)
        return True


class FakeOriginal:
    model = "test-model"


def _make_gen(upstream, request_id="rid-test"):
    import logging

    return convert_responses_streaming_to_claude_with_cancellation(
        upstream,
        FakeOriginal(),
        logging.getLogger("test"),
        FakeRequest(),
        FakeClient(),
        request_id,
    )


async def _collect(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def _parse_frames(blob):
    """Split concatenated SSE frames into (event, payload) pairs.

    Comment pings (': ping') carry no event and parse to (None, None).
    """
    frames = []
    for raw in blob.split("\n\n"):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith(":"):
            frames.append((None, None))
            continue
        for line in raw.split("\n"):
            if line.startswith("event:"):
                etype = line[6:].strip()
                break
        else:
            continue
        _, payload = _split_event(raw)
        frames.append((etype, payload))
    return frames


def test_optimistic_start_before_upstream():
    """Initial burst must not wait on upstream TTFT."""

    async def run():
        release = asyncio.Event()

        async def slow_upstream():
            await release.wait()
            yield 'event: response.output_text.delta\ndata: {"delta": "hi"}'
            yield (
                "event: response.completed\n"
                'data: {"response": {"status": "completed", "output": [], '
                '"usage": {"input_tokens": 1, "output_tokens": 1, '
                '"input_tokens_details": {}}}}'
            )

        gen = _make_gen(slow_upstream())
        first = []
        for _ in range(4):
            first.append(await asyncio.wait_for(gen.__anext__(), timeout=5))
        blob = "".join(first)
        assert "message_start" in blob
        assert "content_block_start" in blob
        assert "ping" in blob
        assert not release.is_set(), "upstream must not have been awaited yet"
        release.set()
        rest = await _collect(gen)
        assert "message_stop" in "".join(rest)

    asyncio.run(run())


def test_post_header_429_becomes_sse_error():
    """Upstream 429 after headers -> SSE error event, no exception."""

    async def run():
        async def failing_upstream():
            raise HTTPException(
                status_code=429,
                detail="429 FreeUsageLimitError: free tier rate limit exceeded",
            )
            yield  # pragma: no cover - makes this an async generator

        frames = _parse_frames("".join(await _collect(_make_gen(failing_upstream()))))
        errors = [p for e, p in frames if e == "error"]
        assert errors, f"expected SSE error event, got {frames}"
        assert "Free-tier rate limit exceeded" in errors[0]["error"]["message"]

    asyncio.run(run())


def test_post_header_500_becomes_sse_error():
    async def run():
        async def failing_upstream():
            raise HTTPException(status_code=500, detail="Internal Server Error")
            yield  # pragma: no cover

        frames = _parse_frames("".join(await _collect(_make_gen(failing_upstream()))))
        errors = [p for e, p in frames if e == "error"]
        assert errors, f"expected SSE error event, got {frames}"
        assert "Internal Server Error" in errors[0]["error"]["message"]

    asyncio.run(run())


def test_cancel_still_yields_cancelled():
    async def run():
        async def failing_upstream():
            raise HTTPException(status_code=499, detail="Request cancelled by client")
            yield  # pragma: no cover

        frames = _parse_frames("".join(await _collect(_make_gen(failing_upstream()))))
        errors = [p for e, p in frames if e == "error"]
        assert errors and errors[0]["error"]["type"] == "cancelled"

    asyncio.run(run())


def test_error_event_shape_parseable():
    """The SSE error frame must round-trip through _split_event."""
    raw = (
        'event: error\ndata: {"type": "error", '
        '"error": {"type": "api_error", "message": "boom"}}'
    )
    etype, payload = _split_event(raw)
    assert etype == "error"
    assert payload["error"]["message"] == "boom"
    assert json.loads(raw.split("data: ", 1)[1]) == payload


# --- Phase 3: bytes-framing equivalence -----------------------------------

_SAMPLE_EVENTS = [
    'event: response.created\ndata: {"x": 1}',
    'event: response.output_text.delta\ndata: {"delta": "héllo ✓ 🎉"}',
    ": upstream comment ping",
    'event: response.output_text.delta\ndata: {"delta": "line1"}\ndata: {"delta": "line2"}',
    'event: message\ndata: {"bare": true}',
    "event: no-data-here",
]


def _join_lines(data_lines):
    return "\n".join(data_lines)


def _reference_frames(payload: str):
    """Old aiter_lines-style framing (splitlines ~= universal newlines)."""
    out = []
    event_type = None
    data_lines = []
    for line in payload.splitlines():
        if not line.strip():
            if event_type is not None or data_lines:
                out.append(f"event: {event_type or 'message'}\ndata: {_join_lines(data_lines)}")
            event_type, data_lines = None, []
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if event_type is not None or data_lines:
        out.append(f"event: {event_type or 'message'}\ndata: {_join_lines(data_lines)}")
    return out


def _bytes_frames(payload_bytes: bytes, chunk_sizes):
    """New bytes-path framing under a given chunking."""
    buf = bytearray()
    out = []
    idx = 0
    n = 0
    while idx < len(payload_bytes):
        size = chunk_sizes[n % len(chunk_sizes)]
        n += 1
        buf += payload_bytes[idx:idx + size]
        idx += size
        for text in _extract_sse_events(buf):
            etype, data = _parse_sse_event(text)
            if data is not None:
                out.append(f"event: {etype}\ndata: {data}")
    tail = bytes(buf).decode("utf-8", "replace")
    if tail.strip():
        etype, data = _parse_sse_event(tail)
        if data is not None:
            out.append(f"event: {etype}\ndata: {data}")
    return out


def _sample_payload(line_ending="\n"):
    base = "\n\n".join(_SAMPLE_EVENTS) + "\n\n"
    return base.replace("\n", line_ending) if line_ending != "\n" else base


def test_bytes_framing_matches_lines_lf():
    payload = _sample_payload("\n")
    expected = _reference_frames(payload)
    assert _bytes_frames(payload.encode(), [100000]) == expected


def test_bytes_framing_matches_lines_crlf():
    payload = _sample_payload("\r\n")
    expected = _reference_frames(payload)
    assert _bytes_frames(payload.encode(), [100000]) == expected


def test_bytes_framing_single_byte_chunks():
    payload = _sample_payload("\n").encode()
    expected = _reference_frames(payload.decode())
    assert _bytes_frames(payload, [1]) == expected


def test_bytes_framing_split_crlf_across_chunks():
    payload = _sample_payload("\r\n").encode()
    expected = _reference_frames(payload.decode().replace("\r\n", "\n"))
    # Chunk sizes chosen to split several \r\n sequences across boundaries.
    assert _bytes_frames(payload, [7, 3, 11, 5]) == expected


def test_bytes_framing_random_chunkings():
    rng = random.Random(42)
    payload = _sample_payload("\n").encode()
    expected = _reference_frames(payload.decode())
    for _ in range(20):
        sizes = [rng.randint(1, 37) for _ in range(5)]
        assert _bytes_frames(payload, sizes) == expected


def test_bytes_framing_multibyte_split():
    payload = 'event: t\ndata: {"e": "🎉🎉🎉"}\n\n'.encode()
    expected = _reference_frames(payload.decode())
    for size in (1, 2, 3, 5):
        assert _bytes_frames(payload, [size]) == expected


def _responses_frames(blob):
    frames = {}
    for raw in blob.split("\n\n"):
        raw = raw.strip()
        if not raw or raw.startswith(":"):
            continue
        etype, payload = _split_event(raw)
        if etype:
            frames.setdefault(etype, []).append(payload)
    return frames


def test_delta_before_added_is_adopted():
    """Arg fragments arriving before output_item.added are not lost."""
    import logging

    async def run():
        async def upstream():
            yield (
                'event: response.function_call_arguments.delta\n'
                'data: {"item_id": "fc_9", "delta": "{\\"a\\":"}'
            )
            yield (
                'event: response.output_item.added\n'
                'data: {"item": {"type": "function_call", "id": "fc_9", '
                '"call_id": "call_9", "name": "f", "arguments": ""}}'
            )
            yield (
                'event: response.function_call_arguments.delta\n'
                'data: {"item_id": "fc_9", "delta": "1}"}'
            )
            yield (
                'event: response.completed\n'
                'data: {"response": {"status": "completed", "output": [], '
                '"usage": {"input_tokens": 1, "output_tokens": 1, '
                '"input_tokens_details": {}}}}'
            )

        gen = convert_responses_streaming_to_claude_with_cancellation(
            upstream(),
            FakeOriginal(),
            logging.getLogger("test"),
            FakeRequest(),
            FakeClient(),
            "rid-pending",
        )
        out = []
        async for item in gen:
            out.append(item)
        frames = _responses_frames("".join(out))
        deltas = [
            d["delta"]["partial_json"]
            for d in frames.get("content_block_delta", [])
            if d["delta"].get("type") == "input_json_delta"
        ]
        assert "".join(deltas) == '{"a":1}', deltas

    asyncio.run(run())


def _live_converter_tasks():
    me = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not me and not t.done()}


def test_no_task_leak_on_post_header_error():
    """Failing upstream must not leave pump/keepalive tasks behind."""

    async def run():
        async def upstream():
            for i in range(150):
                yield (
                    'event: response.output_text.delta\n'
                    'data: {"delta": "tok%d"}' % i
                )
            raise HTTPException(status_code=500, detail="boom")

        before = _live_converter_tasks()
        gen = convert_responses_streaming_to_claude_with_cancellation(
            upstream(),
            FakeOriginal(),
            __import__("logging").getLogger("test"),
            FakeRequest(),
            FakeClient(),
            "rid-leak",
        )
        out = [item async for item in gen]
        assert any("error" in o for o in out)
        await asyncio.sleep(0)
        leaked = _live_converter_tasks() - before
        assert not leaked, f"leaked tasks: {leaked}"

    asyncio.run(run())


def test_no_task_leak_on_aclose():
    """Closing the stream early must cancel pump/keepalive."""

    async def run():
        async def upstream():
            for i in range(1000):
                yield (
                    'event: response.output_text.delta\n'
                    'data: {"delta": "tok%d"}' % i
                )
            yield (
                'event: response.completed\n'
                'data: {"response": {"status": "completed", "output": [], '
                '"usage": {}}}'
            )

        before = _live_converter_tasks()
        gen = convert_responses_streaming_to_claude_with_cancellation(
            upstream(),
            FakeOriginal(),
            __import__("logging").getLogger("test"),
            FakeRequest(),
            FakeClient(),
            "rid-aclose",
        )
        for _ in range(6):
            await gen.__anext__()
        await gen.aclose()
        await asyncio.sleep(0)
        leaked = _live_converter_tasks() - before
        assert not leaked, f"leaked tasks: {leaked}"

    asyncio.run(run())


def test_responses_tool_args_stream_incrementally():
    """Arg fragments forward immediately; done emits no duplicate."""
    import logging

    async def run():
        async def upstream():
            yield (
                'event: response.output_item.added\n'
                'data: {"item": {"type": "function_call", "id": "fc_1", '
                '"call_id": "call_1", "name": "weather", "arguments": ""}}'
            )
            yield (
                'event: response.function_call_arguments.delta\n'
                'data: {"item_id": "fc_1", "delta": "{\\"loc\\":"}'
            )
            yield (
                'event: response.function_call_arguments.delta\n'
                'data: {"item_id": "fc_1", "delta": " \\"Paris\\"}"}'
            )
            yield (
                'event: response.function_call_arguments.done\n'
                'data: {"item_id": "fc_1", "arguments": "{\\"loc\\": \\"Paris\\"}"}'
            )
            yield (
                'event: response.completed\n'
                'data: {"response": {"status": "completed", "output": [], '
                '"usage": {"input_tokens": 1, "output_tokens": 1, '
                '"input_tokens_details": {}}}}'
            )

        gen = convert_responses_streaming_to_claude_with_cancellation(
            upstream(),
            FakeOriginal(),
            logging.getLogger("test"),
            FakeRequest(),
            FakeClient(),
            "rid-tool",
        )
        out = []
        async for item in gen:
            out.append(item)
        frames = _responses_frames("".join(out))
        deltas = [
            d["delta"]["partial_json"]
            for d in frames.get("content_block_delta", [])
            if d["delta"].get("type") == "input_json_delta"
        ]
        assert "".join(deltas) == '{"loc": "Paris"}', deltas
        # No duplication: exactly the two fragments, nothing re-sent at done.
        assert len(deltas) == 2, deltas

    asyncio.run(run())


_TESTS = [
    test_optimistic_start_before_upstream,
    test_post_header_429_becomes_sse_error,
    test_post_header_500_becomes_sse_error,
    test_cancel_still_yields_cancelled,
    test_error_event_shape_parseable,
    test_bytes_framing_matches_lines_lf,
    test_bytes_framing_matches_lines_crlf,
    test_bytes_framing_single_byte_chunks,
    test_bytes_framing_split_crlf_across_chunks,
    test_bytes_framing_random_chunkings,
    test_bytes_framing_multibyte_split,
    test_responses_tool_args_stream_incrementally,
    test_delta_before_added_is_adopted,
    test_no_task_leak_on_post_header_error,
    test_no_task_leak_on_aclose,
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
