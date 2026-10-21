"""Transient-failure retry policy tests.

Covers the classification rules (what is and is not worth retrying), the
fixed-interval retrier, and the SSE/JSON payload detectors used by the
passthrough paths.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_retry_policy.py
(no third-party test deps required).
"""

import asyncio
import json
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.retry import (  # noqa: E402
    RetryPolicy,
    Retrier,
    TransientUpstreamError,
    find_sse_error_text,
    find_transient_payload,
    is_rate_limited,
    is_transient_error,
    is_transient_exception,
    is_transient_payload,
    is_transient_status,
    iter_transient_sse_lines,
    retry_settings,
    sse_error_status,
)


# --- classification -------------------------------------------------------

def test_temporarily_unavailable_is_transient():
    """The upstream's own wording for a flapping free-tier model."""
    assert is_transient_error("Model is temporarily unavailable upstream.")
    assert is_transient_error("Model is temporarily unavailable upstream.", 500)
    assert is_transient_error("Error: Endpoint is unavailable")


def test_rate_limit_is_transient():
    assert is_rate_limited("FreeUsageLimitError")
    assert is_rate_limited("Rate limit exceeded. Please try again later.")
    assert is_transient_error("FreeUsageLimitError: Rate limit exceeded", 429)
    assert is_transient_status(429)


def test_client_errors_are_not_transient():
    """A 400/401/403 is the request's fault: retrying burns quota."""
    assert not is_transient_status(400)
    assert not is_transient_status(401)
    assert not is_transient_status(403)
    assert not is_transient_error("invalid_api_key", 401)
    assert not is_transient_error("model not found", 404)


def test_server_errors_are_transient():
    for status in (500, 502, 503, 504):
        assert is_transient_status(status)


def test_connection_failures_are_transient():
    assert is_transient_error("Connection error.")
    assert is_transient_exception(ConnectionError("reset"))
    assert is_transient_exception(TimeoutError("timed out"))


def test_transient_exception_respects_status():
    class StatusError(Exception):
        def __init__(self, status, text):
            self.status_code = status
            super().__init__(text)

    assert is_transient_exception(StatusError(503, "boom"))
    assert not is_transient_exception(StatusError(400, "bad request"))
    # A 4xx whose text is a rate-limit rejection is still transient.
    assert is_transient_exception(StatusError(400, "Rate limit exceeded"))


def test_transient_exception_with_a_200_status():
    """The upstream can answer 200 and only then deliver a rejection.

    A 2xx must not be treated as a permanent failure just because it is
    not in the retryable-status set — the body text decides.
    """
    assert is_transient_exception(
        TransientUpstreamError("Model is temporarily unavailable upstream", 200)
    )
    assert is_transient_exception(
        TransientUpstreamError(
            '{"type":"error","error":{"type":"FreeUsageLimitError",'
            '"message":"Rate limit exceeded. Please try again later."}}',
            200,
        )
    )
    # A 200 with a genuinely permanent body is still not transient.
    assert not is_transient_exception(
        TransientUpstreamError("invalid api key", 200)
    )


# --- payloads -------------------------------------------------------------

def test_find_transient_payload_shapes():
    assert find_transient_payload(
        {"error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded"}}
    ) == "Rate limit exceeded"
    assert find_transient_payload({"error": "boom"}) == "boom"
    assert find_transient_payload({"type": "error", "message": "nope"}) == "nope"
    assert find_transient_payload({"choices": []}) is None
    assert find_transient_payload("not a dict") is None


def test_is_transient_payload():
    assert is_transient_payload(
        {"error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded"}},
        429,
    )
    assert not is_transient_payload({"error": {"message": "invalid api key"}}, 401)


def test_sse_error_status():
    assert sse_error_status(
        json.dumps({"error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded"}})
    ) == 429
    assert sse_error_status(
        json.dumps({"error": {"message": "Model is temporarily unavailable upstream"}})
    ) == 503
    # A normal event is not an error.
    assert sse_error_status(json.dumps({"choices": []})) is None
    assert sse_error_status("not json") is None


def test_find_sse_error_text():
    raw = b'data: {"error": {"message": "Model is temporarily unavailable upstream"}}\n\n'
    assert "temporarily unavailable" in find_sse_error_text(raw)
    # A normal event carries no error.
    assert find_sse_error_text(b'data: {"choices": []}\n\n') is None
    # A partial trailing event is not an error.
    assert find_sse_error_text(b'data: {"choices"') is None


def test_iter_transient_sse_lines():
    lines = [
        'data: {"choices": []}',
        'data: {"error": {"message": "Rate limit exceeded"}}',
    ]
    assert "Rate limit" in iter_transient_sse_lines(lines)
    assert iter_transient_sse_lines(['data: {"choices": []}']) is None


# --- policy ---------------------------------------------------------------

def test_retry_settings_defaults():
    assert retry_settings({}) == (10, 10.0)
    assert retry_settings({"RETRY_ATTEMPTS": "3", "RETRY_INTERVAL_SECS": "2.5"}) == (3, 2.5)
    # Garbage falls back to the defaults rather than crashing the proxy.
    assert retry_settings({"RETRY_ATTEMPTS": "abc"}) == (10, 10.0)
    assert retry_settings({"RETRY_INTERVAL_SECS": "abc"}) == (10, 10.0)
    # Never below one attempt.
    assert retry_settings({"RETRY_ATTEMPTS": "0"}) == (1, 10.0)


def test_policy_should_retry():
    policy = RetryPolicy(attempts=3, interval=0)
    err = TransientUpstreamError("Model is temporarily unavailable upstream", 503)
    assert policy.should_retry(1, err)
    assert policy.should_retry(2, err)
    assert not policy.should_retry(3, err)
    # A permanent failure is never retried.
    assert not policy.should_retry(1, TransientUpstreamError("invalid api key", 401))


def test_retrier_succeeds_after_transient_failures():
    """The core requirement: a transient failure is retried, not surfaced."""
    calls = []

    async def operation(attempt):
        calls.append(attempt)
        if attempt < 3:
            raise TransientUpstreamError(
                "Model is temporarily unavailable upstream", 503
            )
        return "ok"

    retrier = Retrier(attempts=10, interval=0)
    result = asyncio.run(retrier.run(operation))
    assert result == "ok"
    assert calls == [1, 2, 3]


def test_retrier_gives_up_after_attempts():
    calls = []

    async def operation(attempt):
        calls.append(attempt)
        raise TransientUpstreamError("Model is temporarily unavailable upstream", 503)

    retrier = Retrier(attempts=4, interval=0)
    try:
        asyncio.run(retrier.run(operation))
    except TransientUpstreamError:
        pass
    else:
        raise AssertionError("expected the transient error to surface")
    assert calls == [1, 2, 3, 4]


def test_retrier_does_not_retry_permanent_failures():
    calls = []

    async def operation(attempt):
        calls.append(attempt)
        raise TransientUpstreamError("invalid api key", 401)

    retrier = Retrier(attempts=10, interval=0)
    try:
        asyncio.run(retrier.run(operation))
    except TransientUpstreamError:
        pass
    else:
        raise AssertionError("expected the permanent error to surface")
    assert calls == [1]


def test_retrier_waits_the_configured_interval():
    """Retries are spaced by a fixed interval, not back-to-back."""
    slept = []
    real_sleep = asyncio.sleep

    async def operation(attempt):
        if attempt < 3:
            raise TransientUpstreamError("Model is temporarily unavailable", 503)
        return "ok"

    async def run():
        retrier = Retrier(attempts=10, interval=10.0)

        async def sleeper():
            # Record the interval the policy actually waited.
            slept.append(retrier.policy.interval)
            await real_sleep(0)

        # Patch the policy's sleep, not asyncio.sleep, to keep the test
        # instant while still asserting the interval that was used.
        retrier.policy.sleep = sleeper
        return await retrier.run(operation)

    assert asyncio.run(run()) == "ok"
    assert slept == [10.0, 10.0]


def test_retrier_on_retry_hook_fires_between_attempts():
    seen = []

    async def on_retry(attempt, exc):
        seen.append(attempt)

    async def operation(attempt):
        if attempt < 3:
            raise TransientUpstreamError("Model is temporarily unavailable", 503)
        return "ok"

    retrier = Retrier(attempts=10, interval=0, on_retry=on_retry)
    assert asyncio.run(retrier.run(operation)) == "ok"
    assert seen == [1, 2]


_TESTS = [
    test_temporarily_unavailable_is_transient,
    test_rate_limit_is_transient,
    test_client_errors_are_not_transient,
    test_server_errors_are_transient,
    test_connection_failures_are_transient,
    test_transient_exception_respects_status,
    test_find_transient_payload_shapes,
    test_is_transient_payload,
    test_sse_error_status,
    test_find_sse_error_text,
    test_iter_transient_sse_lines,
    test_retry_settings_defaults,
    test_policy_should_retry,
    test_retrier_succeeds_after_transient_failures,
    test_retrier_gives_up_after_attempts,
    test_retrier_does_not_retry_permanent_failures,
    test_retrier_waits_the_configured_interval,
    test_retrier_on_retry_hook_fires_between_attempts,
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
