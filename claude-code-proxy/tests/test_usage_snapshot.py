"""message_start usage-base tests: continuous live token counter.

Regression test: the proxy sent ``usage: {input_tokens: 0, ...}`` in every
``message_start`` while real Anthropic sends the true cumulative input
counts there. The CLI builds its live context counter from message_start
and only corrects it at message_delta, so the displayed counter collapsed
to ~zero at each turn start and jumped back at turn end.

The proxy now reports the previous completed response's totals as the
next turn's message_start base.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_usage_snapshot.py
(no third-party test deps required).
"""

import asyncio
import json
import logging
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversion.response_responses import (  # noqa: E402
    convert_responses_streaming_to_claude_with_cancellation,
)
from src.core.stats import _MAX_USAGE_KEYS, request_usage_key, stats  # noqa: E402
from src.models.claude import (  # noqa: E402
    ClaudeMessage,
    ClaudeMessagesRequest,
)

logger = logging.getLogger("test")


class FakeRequest:
    async def is_disconnected(self):
        return False


class FakeClient:
    def cancel_request(self, request_id):
        return True


def _snapshot():
    return dict(stats._last_usage)


def _restore(snap):
    stats._last_usage = dict(snap)


async def _fake_upstream(input_tokens=47000, output_tokens=300, cached=46000):
    yield (
        "event: response.output_text.delta\n"
        'data: {"type": "x", "delta": "hi"}'
    )
    yield (
        "event: response.completed\n"
        + "data: "
        + json.dumps(
            {
                "type": "x",
                "response": {
                    "status": "completed",
                    "usage": {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "input_tokens_details": {"cached_tokens": cached},
                    },
                },
            }
        )
    )


async def _run_stream(text="hi"):
    request = ClaudeMessagesRequest(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[ClaudeMessage(role="user", content=text)],
    )
    raw = []
    async for ev in convert_responses_streaming_to_claude_with_cancellation(
        _fake_upstream(), request, logger, FakeRequest(), FakeClient(), "rid-u"
    ):
        raw.append(ev)
    events = []
    for r in raw:
        et, data = None, None
        for line in r.split("\n"):
            if line.startswith("event:"):
                et = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if data and data != "[DONE]":
            events.append((et, json.loads(data)))
    return events


def _request_for(text="hi"):
    return ClaudeMessagesRequest(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[ClaudeMessage(role="user", content=text)],
    )


def test_message_start_carries_previous_base():
    snap = _snapshot()
    try:
        key = request_usage_key(_request_for())
        stats.note_response_usage(
            {
                "input_tokens": 47082,
                "output_tokens": 372,
                "cache_read_input_tokens": 43505,
                "cache_creation_input_tokens": 0,
            },
            key=key,
        )
        events = asyncio.run(_run_stream())
        start = next(d for e, d in events if e == "message_start")
        usage = start["message"]["usage"]
        assert usage["input_tokens"] == 47082, usage
        assert usage["cache_read_input_tokens"] == 43505, usage
        assert usage["output_tokens"] == 0, usage  # new turn, nothing yet
        # Turn end snapshots the fresh totals for the following turn.
        assert stats._last_usage[key]["input_tokens"] == 47000, stats._last_usage
        delta = next(d for e, d in events if e == "message_delta")
        assert delta["usage"]["input_tokens"] == 47000, delta["usage"]
    finally:
        _restore(snap)


def test_first_turn_starts_at_zero():
    snap = _snapshot()
    try:
        stats._last_usage = {}
        events = asyncio.run(_run_stream())
        start = next(d for e, d in events if e == "message_start")
        assert start["message"]["usage"]["input_tokens"] == 0
    finally:
        _restore(snap)


def test_concurrent_sessions_keep_separate_bases():
    """Two sessions sharing one proxy must not borrow each other's base."""
    snap = _snapshot()
    try:
        stats._last_usage = {}
        asyncio.run(_run_stream(text="session A work"))
        asyncio.run(_run_stream(text="session B work"))
        assert len(stats._last_usage) == 2, stats._last_usage
        bases = {asyncio.run(_start_base(t)) for t in ("session A work", "session B work")}
        # Distinct conversations resolve to distinct keys.
        ka = request_usage_key(
            ClaudeMessagesRequest(
                model="claude-opus-4-8",
                max_tokens=100,
                messages=[ClaudeMessage(role="user", content="session A work")],
            )
        )
        kb = request_usage_key(
            ClaudeMessagesRequest(
                model="claude-opus-4-8",
                max_tokens=100,
                messages=[ClaudeMessage(role="user", content="session B work")],
            )
        )
        assert ka != kb
        assert stats._last_usage[ka]["input_tokens"] == 47000
        assert stats._last_usage[kb]["input_tokens"] == 47000
        assert bases == {47000}
    finally:
        _restore(snap)


async def _start_base(text):
    request = ClaudeMessagesRequest(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[ClaudeMessage(role="user", content=text)],
    )
    async for raw in convert_responses_streaming_to_claude_with_cancellation(
        _fake_upstream(), request, logger, FakeRequest(), FakeClient(), "rid-u"
    ):
        for line in raw.split("\n"):
            if line.startswith("data:"):
                data = line[5:].strip()
                if data.startswith("{"):
                    obj = json.loads(data)
                    if obj.get("type") == "message_start":
                        return obj["message"]["usage"]["input_tokens"]
    raise AssertionError("no message_start")


def test_key_stable_across_turns_but_turn_sensitive():
    def key_for(extra=(), last="q"):
        return request_usage_key(
            ClaudeMessagesRequest(
                model="claude-opus-4-8",
                max_tokens=100,
                messages=[
                    ClaudeMessage(role="user", content="first"),
                    *extra,
                    ClaudeMessage(role="user", content=last),
                ],
            )
        )

    assert key_for() == key_for()  # deterministic
    # Same conversation, grown by later turns -> SAME key (prefix only).
    grown = key_for(
        extra=(
            ClaudeMessage(role="assistant", content="a1"),
            ClaudeMessage(role="user", content="q2"),
            ClaudeMessage(role="assistant", content="a2"),
        ),
        last="q3",
    )
    assert grown == key_for(), (grown, key_for())
    # Different opening -> different conversation -> different key.
    other = request_usage_key(
        ClaudeMessagesRequest(
            model="claude-opus-4-8",
            max_tokens=100,
            messages=[ClaudeMessage(role="user", content="something else")],
        )
    )
    assert other != key_for()


def test_lru_cap_evicts_oldest():
    snap = _snapshot()
    try:
        stats._last_usage = {}
        for i in range(_MAX_USAGE_KEYS + 4):
            stats.note_response_usage({"input_tokens": i}, key=f"k{i}")
        assert len(stats._last_usage) == _MAX_USAGE_KEYS, len(stats._last_usage)
        assert "k0" not in stats._last_usage
        assert f"k{_MAX_USAGE_KEYS + 3}" in stats._last_usage
    finally:
        _restore(snap)


if __name__ == "__main__":
    test_message_start_carries_previous_base()
    test_first_turn_starts_at_zero()
    test_concurrent_sessions_keep_separate_bases()
    test_key_stable_across_turns_but_turn_sensitive()
    test_lru_cap_evicts_oldest()
    print("test_usage_snapshot: all 5 tests passed")
