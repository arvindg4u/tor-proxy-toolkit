"""Thinking-display tests: upstream reasoning summaries -> Claude thinking.

Regression test: the proxy used to request reasoning WITHOUT
``summary: "auto"`` (opt-in upstream) and dropped reasoning items, so long
reasoning phases streamed nothing but keepalive pings — the CLI showed a
frozen spinner / dead token counter, then everything in one shot.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_thinking_stream.py
(no third-party test deps required).
"""

import asyncio
import json
import logging
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversion.request_responses import convert_claude_to_responses  # noqa: E402
from src.conversion.response_responses import (  # noqa: E402
    convert_responses_streaming_to_claude_with_cancellation,
    convert_responses_to_claude_response,
)
from src.models.claude import (  # noqa: E402
    ClaudeMessage,
    ClaudeMessagesRequest,
    ClaudeThinkingConfig,
)

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


class StubModelManager:
    @staticmethod
    def map_claude_model_to_openai(model):
        return "test-model"


def _sse_event(event_type, payload):
    return f"event: {event_type}\ndata: {json.dumps(payload)}"


async def _fake_upstream():
    yield _sse_event(
        "response.reasoning_summary_part.added", {"type": "x", "item_id": "rs_1"}
    )
    yield _sse_event(
        "response.reasoning_summary_text.delta",
        {"type": "x", "delta": "Framing a concise"},
    )
    yield _sse_event(
        "response.reasoning_summary_text.delta",
        {"type": "x", "delta": " one-sentence answer"},
    )
    yield _sse_event(
        "response.output_item.done",
        {"type": "x", "item": {"type": "reasoning", "id": "rs_1"}},
    )
    yield _sse_event(
        "response.output_text.delta", {"type": "x", "delta": "The sky is blue."}
    )
    yield _sse_event(
        "response.completed",
        {
            "type": "x",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 25},
            },
        },
    )


def _collect_events(raw_events):
    parsed = []
    for raw in raw_events:
        event_type, data_str = None, None
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_str = line[5:].strip()
        if data_str and data_str != "[DONE]":
            parsed.append((event_type, json.loads(data_str)))
    return parsed


def _request(thinking):
    return ClaudeMessagesRequest(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[ClaudeMessage(role="user", content="hi")],
        thinking=thinking,
    )


async def _run_stream(thinking):
    request = _request(thinking)
    out = []
    async for raw in convert_responses_streaming_to_claude_with_cancellation(
        _fake_upstream(), request, logger, FakeRequest(), FakeClient(), "rid-1"
    ):
        out.append(raw)
    return _collect_events(out)


def test_summary_requested_only_when_thinking_enabled():
    enabled = convert_claude_to_responses(
        _request(ClaudeThinkingConfig(type="enabled")), StubModelManager()
    )
    assert enabled["reasoning"]["summary"] == "auto", enabled.get("reasoning")
    disabled = convert_claude_to_responses(
        _request(ClaudeThinkingConfig(type="disabled")), StubModelManager()
    )
    assert "summary" not in disabled["reasoning"], disabled.get("reasoning")
    none_req = convert_claude_to_responses(_request(None), StubModelManager())
    assert "reasoning" not in none_req


def test_reasoning_streams_as_thinking_block():
    events = asyncio.run(_run_stream(ClaudeThinkingConfig(type="enabled")))
    starts = [
        d for e, d in events
        if e == "content_block_start"
        and d.get("content_block", {}).get("type") == "thinking"
    ]
    assert len(starts) == 1, events
    thinking_idx = next(
        d["index"] for e, d in events
        if e == "content_block_start"
        and d.get("content_block", {}).get("type") == "thinking"
    )
    deltas = [
        d["delta"]["thinking"] for e, d in events
        if e == "content_block_delta"
        and d.get("delta", {}).get("type") == "thinking_delta"
    ]
    assert deltas == ["Framing a concise", " one-sentence answer"], deltas
    assert all(
        d["index"] == thinking_idx for e, d in events
        if e == "content_block_delta"
        and d.get("delta", {}).get("type") == "thinking_delta"
    )
    stops = [
        d["index"] for e, d in events if e == "content_block_stop"
    ]
    assert thinking_idx in stops, stops
    texts = [
        d["delta"]["text"] for e, d in events
        if e == "content_block_delta"
        and d.get("delta", {}).get("type") == "text_delta"
    ]
    assert texts == ["The sky is blue."], texts
    usage = next(
        d["usage"] for e, d in events if e == "message_delta"
    )
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 25, usage


def test_no_thinking_when_disabled():
    events = asyncio.run(_run_stream(ClaudeThinkingConfig(type="disabled")))
    thinking_starts = [
        d for e, d in events
        if e == "content_block_start"
        and d.get("content_block", {}).get("type") == "thinking"
    ]
    assert thinking_starts == [], events
    texts = [
        d["delta"]["text"] for e, d in events
        if e == "content_block_delta"
        and d.get("delta", {}).get("type") == "text_delta"
    ]
    assert texts == ["The sky is blue."], texts


def test_non_streaming_thinking_parity():
    request = _request(ClaudeThinkingConfig(type="enabled"))
    resp = {
        "id": "resp_1",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [
                    {"type": "summary_text", "text": "Considering options"},
                    {"type": "summary_text", "text": "Picking one"},
                ],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        ],
        "usage": {"input_tokens": 3, "output_tokens": 9},
    }
    out = convert_responses_to_claude_response(resp, request)
    assert out["content"][0]["type"] == "thinking", out["content"]
    assert "Considering options" in out["content"][0]["thinking"]
    assert out["content"][1] == {"type": "text", "text": "Done."}


if __name__ == "__main__":
    test_summary_requested_only_when_thinking_enabled()
    test_reasoning_streams_as_thinking_block()
    test_no_thinking_when_disabled()
    test_non_streaming_thinking_parity()
    print("test_thinking_stream: all 4 tests passed")
