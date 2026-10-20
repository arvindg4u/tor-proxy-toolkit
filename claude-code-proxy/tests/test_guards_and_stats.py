"""Review-fix regression tests: guards, stream-error stats, stat guards.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_guards_and_stats.py
(no third-party test deps required).
"""

import json
import math
import os
import sys
import tempfile

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.failure_log import record_upstream_failure  # noqa: E402
from src.core.guards import (  # noqa: E402
    body_too_large,
    sanitize_passthrough_path,
    scrub_api_keys,
)
from src.core.stats import ProxyStats  # noqa: E402


def test_scrub_api_keys():
    assert scrub_api_keys("key sk-abcDEF123_xyz hello") == "key sk-*** hello"
    assert scrub_api_keys("no keys here") == "no keys here"
    assert scrub_api_keys("") == ""
    assert scrub_api_keys(None) == ""
    # Upstream OpenCode key and underscore form must not leak either.
    assert "TESTONLY" not in scrub_api_keys("upstream oc_sk_TESTONLY123456_secret")
    assert scrub_api_keys("upstream oc_sk_TESTONLY123456_secret") == "upstream sk-***"
    # Upstream-masked fragments still scrubbed.
    assert "sk-" not in scrub_api_keys("err sk-test-*ummy end").replace("sk-***", "")


def test_sanitize_passthrough_path():
    assert sanitize_passthrough_path("") == ""
    assert sanitize_passthrough_path("abc123") == "abc123"
    assert sanitize_passthrough_path("a/b-c_d") == "a/b-c_d"
    # Encoded separator normalizes harmlessly (no traversal possible).
    assert sanitize_passthrough_path("a%2fb") == "a/b"
    for bad in (
        "..",
        "../x",
        "a/../b",
        "%2e%2e/x",
        "%252e",
        "a?b",
        "a#b",
        "a\\b",
        "a\x00b",
    ):
        try:
            sanitize_passthrough_path(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"path accepted but should reject: {bad!r}")


def test_body_too_large():
    assert body_too_large({"content-length": "11"}, 10) is True
    assert body_too_large({"content-length": "10"}, 10) is False
    assert body_too_large({}, 10) is False
    assert body_too_large({"content-length": "garbage"}, 10) is False
    assert body_too_large({"content-length": "-5"}, 10) is False


def test_record_stream_error():
    s = ProxyStats()
    s.record("/v1/messages", status=200)
    total = s.snapshot()["total_requests"]
    s.record_stream_error(429)
    snap = s.snapshot()
    assert snap["total_requests"] == total, "stream errors must not double-count"
    assert snap["errors"] == 1
    assert snap["stream_errors"] == 1
    assert snap["ok_requests"] == 0, "the optimistic 200 must become the error"
    assert snap["by_status"].get("429") == 1
    assert "200" not in snap["by_status"], "by_status must reflect final outcomes"
    assert snap["last_error_at"] is not None


def test_stat_input_guards():
    s = ProxyStats()
    s.record_ttft(float("nan"))
    s.record_ttft(float("inf"))
    s.record_ttft(-1.0)
    s.record_ttft("0.1")
    s.record_itl(float("nan"))
    s.record_itl(-2.0)
    s.record_keepalive(0)
    s.record_keepalive(-5)
    snap = s.snapshot()
    assert snap["ttft_ms"]["count"] == 0
    assert snap["itl_ms"]["count"] == 0
    assert snap["keepalive_sent"] == 0
    # Sane values still recorded.
    s.record_ttft(0.5)
    s.record_itl(0.05)
    s.record_keepalive(2)
    snap = s.snapshot()
    assert snap["ttft_ms"]["count"] == 1
    assert snap["itl_ms"]["count"] == 1
    assert snap["keepalive_sent"] == 2
    assert math.isfinite(snap["ttft_ms"]["avg_ms"])


def test_by_model_cardinality_cap():
    s = ProxyStats()
    for i in range(300):
        s.note_model(f"gpt-evil-{i}")
    assert len(s.snapshot()["by_model"]) <= 200


def test_failure_log_archives_and_scrubs():
    previous = os.environ.get("PROXY_LOG_DIR")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["PROXY_LOG_DIR"] = tmp
            record_upstream_failure(
                429, "quota for sk-abcDEF123_xyz", model="m1", request_id="r1"
            )
            record_upstream_failure(500, "boom", model="m2", request_id="r2")
            last_path = os.path.join(tmp, "last_upstream_failure.json")
            with open(last_path, encoding="utf-8") as fh:
                last = json.load(fh)
            assert last["status"] == 500
            assert last["error"] == "boom"
            history = os.listdir(os.path.join(tmp, "failures"))
            assert len(history) == 1, history
            with open(
                os.path.join(tmp, "failures", history[0]), encoding="utf-8"
            ) as fh:
                archived = json.load(fh)
            assert archived["status"] == 429
            assert "abcDEF" not in archived["error"]
            assert "sk-***" in archived["error"]
    finally:
        if previous is None:
            os.environ.pop("PROXY_LOG_DIR", None)
        else:
            os.environ["PROXY_LOG_DIR"] = previous


_TESTS = [
    test_scrub_api_keys,
    test_sanitize_passthrough_path,
    test_body_too_large,
    test_record_stream_error,
    test_stat_input_guards,
    test_by_model_cardinality_cap,
    test_failure_log_archives_and_scrubs,
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
