"""Phase 0 SSE framing + streaming metrics tests.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_sse_framing.py
(no third-party test deps required).
"""

import json
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversion.response_responses import _split_event, _sse  # noqa: E402
from src.core.constants import Constants  # noqa: E402
from src.core.http_client import get_stream_timeout  # noqa: E402
from src.core.stats import ProxyStats  # noqa: E402


def test_sse_format():
    out = _sse("ping", {"type": "ping"})
    assert out.startswith("event: ping\n")
    assert out.endswith("\n\n")
    _, payload = _split_event(out)
    assert payload == {"type": "ping"}


def test_sse_unicode_not_escaped():
    out = _sse("message_start", {"text": "héllo ✓"})
    assert "héllo ✓" in out
    _, payload = _split_event(out)
    assert payload["text"] == "héllo ✓"


def test_split_event_round_trip():
    raw = 'event: response.output_text.delta\ndata: {"delta": "hi"}'
    etype, payload = _split_event(raw)
    assert etype == "response.output_text.delta"
    assert payload == {"delta": "hi"}
    rebuilt = f"event: {etype}\ndata: {json.dumps(payload)}"
    etype2, payload2 = _split_event(rebuilt)
    assert (etype2, payload2) == (etype, payload)


def test_split_event_multiline_data():
    raw = "event: message\ndata: {\"a\": 1}\ndata: {\"b\": 2}"
    # Joined with newline -> invalid JSON -> None (documents current behavior)
    etype, payload = _split_event(raw)
    assert etype == "message"
    assert payload is None


def test_split_event_done_and_invalid():
    etype, payload = _split_event("event: message\ndata: [DONE]")
    assert payload is None
    etype, payload = _split_event("event: message\ndata: not-json{{{")
    assert payload is None
    etype, payload = _split_event("data: {\"x\": 1}")
    assert etype == "message"
    assert payload == {"x": 1}


def test_keepalive_shapes():
    event_ping = _sse(Constants.EVENT_PING, {"type": Constants.EVENT_PING})
    assert event_ping.startswith(f"event: {Constants.EVENT_PING}\n")
    comment_ping = ": ping\n\n"
    assert comment_ping.startswith(":")
    # SSE comments must not parse as events
    etype, payload = _split_event(comment_ping)
    assert payload is None


def test_stats_ttft_itl_keepalive():
    s = ProxyStats()
    snap0 = s.snapshot()
    assert snap0["ttft_ms"]["count"] == 0
    assert snap0["keepalive_sent"] == 0

    s.record_stream()
    s.record_ttft(0.25)
    s.record_ttft(0.75)
    s.record_itl(0.05)
    s.record_keepalive()
    s.record_keepalive(2)

    snap = s.snapshot()
    assert snap["streams"] == 1
    assert snap["ttft_ms"]["count"] == 2
    assert snap["ttft_ms"]["avg_ms"] == 500.0
    assert snap["ttft_ms"]["min_ms"] == 250.0
    assert snap["ttft_ms"]["max_ms"] == 750.0
    assert snap["itl_ms"]["count"] == 1
    assert snap["itl_ms"]["avg_ms"] == 50.0
    assert snap["keepalive_sent"] == 3


def test_stream_timeout_disables_read_timeout():
    t = get_stream_timeout()
    assert t.connect == 5.0
    assert t.read is None
    assert t.write == 10.0
    assert t.pool == 5.0


def test_snapshot_backward_compat_keys():
    snap = ProxyStats().snapshot()
    for key in (
        "uptime_secs",
        "total_requests",
        "ok_requests",
        "errors",
        "avg_latency_ms",
        "tokens_in",
        "tokens_out",
        "by_endpoint",
        "by_model",
        "by_status",
        "streams",
        "ttft_ms",
        "itl_ms",
        "keepalive_sent",
    ):
        assert key in snap, f"missing snapshot key: {key}"


_TESTS = [
    test_sse_format,
    test_sse_unicode_not_escaped,
    test_split_event_round_trip,
    test_split_event_multiline_data,
    test_split_event_done_and_invalid,
    test_keepalive_shapes,
    test_stats_ttft_itl_keepalive,
    test_stream_timeout_disables_read_timeout,
    test_snapshot_backward_compat_keys,
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
