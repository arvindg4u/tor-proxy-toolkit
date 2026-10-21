"""Shared retry policy for transient upstream failures.

The upstream is a free-tier gateway (OpenCode ZEN) whose models come and go:
a request can be rejected with "Model is temporarily unavailable upstream",
answered with a bare 5xx, cut mid-stream with a connection error, or refused
outright because the tier's rate window is exhausted. All of those are
transient — the next attempt, a few seconds later, usually works. Surfacing
them to the client ends the user's turn with a visible error, so the proxy
absorbs them instead.

Two rules make that safe:

* **Only transient failures are retried.** A 400/401/403 is a request or
  credential problem: retrying it just burns the free tier's quota.
* **A fixed interval, not exponential backoff.** The free tier recovers on a
  wall-clock window, so a fixed ``RETRY_INTERVAL_SECS`` (10s by default)
  matches how long the caller is willing to wait. Exponential backoff would
  stretch the wait past the point where the client has given up.

The retry count and interval come from the environment so the operator can
tune them without a code change (``RETRY_ATTEMPTS`` / ``RETRY_INTERVAL_SECS``).

While waiting, the caller may emit a *keepalive tick* (see
:class:`RetryPolicy`). A streaming client that sees no bytes for a while
kills the connection, so a retry loop that stayed silent would be cut off
before it ever recovered.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Iterable, Optional, Tuple

# Upstream error text that means "come back in a moment", not "this request
# is wrong". Matched against the lowercased error body/message.
_TRANSIENT_TEXT = (
    "temporarily unavailable",
    "model is temporarily",
    "endpoint is unavailable",
    "service unavailable",
    "serviceunavailable",
    "bad gateway",
    "gateway timeout",
    "timed out",
    "timeout",
    "connection error",
    "connection reset",
    "connection aborted",
    "server disconnected",
    "peer closed connection",
    "incomplete chunked read",
    "remote end closed connection",
    "overloaded",
    "capacity",
)

# HTTP statuses worth retrying. 429 is included on purpose: the free tier
# answers 429 while its window refills, and the operator's own rotation
# watcher clears a peer-scoped 429 within seconds. A 4xx that is not 429 is
# a client mistake and is never retried.
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

# Body fragments that identify a rate-limit rejection regardless of status.
_RATE_LIMIT_TEXT = (
    "freeusagelimiterror",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "quota",
)


def is_rate_limited(text: str) -> bool:
    """True when the error text carries a rate-limit / quota rejection."""
    low = (text or "").lower()
    return any(frag in low for frag in _RATE_LIMIT_TEXT)


def is_transient_status(status: Optional[int]) -> bool:
    """True for HTTP statuses worth another attempt."""
    if status is None:
        return False
    return status in _RETRYABLE_STATUSES or 500 <= status < 600


def is_transient_error(text: str, status: Optional[int] = None) -> bool:
    """True when an upstream failure looks worth another attempt.

    ``text`` is the upstream error body or exception message; ``status`` is
    the HTTP status when there is one. A rate-limit rejection is transient
    (the window refills), a 4xx is not, and the free tier's "model is
    temporarily unavailable" wording is transient at any status.
    """
    low = (text or "").lower()
    if is_rate_limited(low):
        return True
    if any(frag in low for frag in _TRANSIENT_TEXT):
        return True
    return is_transient_status(status)


def status_from_exception(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status carried by an exception, if any."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def is_transient_exception(exc: BaseException) -> bool:
    """True when an exception describes a transient upstream failure."""
    status = status_from_exception(exc)
    if isinstance(status, int) and 400 <= status < 500:
        # A 4xx is a client/credential error: never retry — UNLESS the body
        # says it is a rate-limit rejection, whose window refills on its own.
        # (A 2xx is not covered here: the upstream can answer 200 and only
        # then deliver a rejection inside the body or the stream.)
        if not is_rate_limited(str(exc)):
            return False
    if is_transient_error(str(exc), status):
        return True
    # Connection-level failures carry no HTTP status but are always worth
    # another attempt.
    return isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError))


class RetryPolicy:
    """Fixed-interval retry policy for transient upstream failures.

    ``attempts`` counts total attempts (1 initial try + N-1 retries) and
    ``interval`` is the fixed wait between them. ``is_transient`` overrides
    the default classification.
    """

    def __init__(
        self,
        attempts: int = 10,
        interval: float = 10.0,
        is_transient: Optional[Callable[[BaseException], bool]] = None,
    ):
        self.attempts = max(1, int(attempts))
        self.interval = max(0.0, float(interval))
        self._is_transient = is_transient or is_transient_exception

    def is_transient(self, exc: BaseException) -> bool:
        """True when ``exc`` is worth another attempt."""
        return bool(self._is_transient(exc))

    def should_retry(self, attempt: int, exc: BaseException) -> bool:
        """True when attempt number ``attempt`` failed but budget remains."""
        return attempt < self.attempts and self.is_transient(exc)

    async def sleep(self) -> None:
        """Wait the fixed interval before the next attempt."""
        if self.interval > 0:
            await asyncio.sleep(self.interval)


class Retrier:
    """Runs an operation again while it keeps failing transiently.

    ``operation`` is awaited with a 1-based attempt number and must raise on
    failure. ``on_retry`` is awaited between attempts — use it to emit a
    keepalive tick so a streaming client does not time out while we wait.
    """

    def __init__(
        self,
        attempts: int = 10,
        interval: float = 10.0,
        is_transient: Optional[Callable[[BaseException], bool]] = None,
        on_retry: Optional[Callable[[int, BaseException], Awaitable[None]]] = None,
    ):
        self.policy = RetryPolicy(attempts, interval, is_transient)
        self.on_retry = on_retry

    @property
    def attempts(self) -> int:
        return self.policy.attempts

    @property
    def interval(self) -> float:
        return self.policy.interval

    async def run(self, operation: Callable[[int], Awaitable[Any]]) -> Any:
        """Await ``operation`` until it succeeds or the attempts run out."""
        last_error: Optional[BaseException] = None
        for attempt in range(1, self.policy.attempts + 1):
            try:
                return await operation(attempt)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise
                last_error = exc
                if not self.policy.should_retry(attempt, exc):
                    raise
                if self.on_retry is not None:
                    await self.on_retry(attempt, exc)
                await self.policy.sleep()
        raise RetryExhausted(self.policy.attempts, last_error)


class RetryExhausted(Exception):
    """Raised when every retry attempt has been used up.

    ``last_error`` carries the final upstream failure so the caller can
    report it verbatim.
    """

    def __init__(self, attempts: int, last_error: Any = None):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"retry budget exhausted after {attempts} attempt(s): {last_error}"
        )


class TransientUpstreamError(Exception):
    """A retryable upstream failure raised by the callers that poll raw HTTP.

    The SDK-based clients raise their own exception types; the passthrough
    paths talk to the upstream with ``httpx`` directly, so they raise this
    instead. ``status`` carries the HTTP status when there is one, which
    lets :func:`is_transient_exception` classify a bare 5xx correctly.
    """

    def __init__(self, message: str, status: Optional[int] = None):
        self.status = status
        super().__init__(message)


def retry_settings(env: Optional[dict] = None) -> Tuple[int, float]:
    """Read ``(attempts, interval)`` from the environment.

    ``RETRY_ATTEMPTS`` counts total attempts (default 10, i.e. 1 initial
    try plus 9 retries). ``RETRY_INTERVAL_SECS`` is the fixed wait between
    attempts (default 10s).

    ``env`` defaults to the process environment, so a caller that passes
    nothing gets the operator's configured values.
    """
    source = os.environ if env is None else env
    try:
        attempts = int(source.get("RETRY_ATTEMPTS", 10))
    except (TypeError, ValueError):
        attempts = 10
    try:
        interval = float(source.get("RETRY_INTERVAL_SECS", 10.0))
    except (TypeError, ValueError):
        interval = 10.0
    return max(1, attempts), max(0.0, interval)


def find_transient_payload(payload: Any) -> Optional[str]:
    """Return the error text of an upstream JSON body, if it has one.

    Recognises the shapes the upstream uses: ``{"error": {...}}`` (OpenAI
    style), ``{"error": "text"}``, and ``{"type": "error", "error": {...}}``
    (the Anthropic-style body ZEN returns).
    """
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "type"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    if isinstance(error, str) and error:
        return error
    if payload.get("type") == "error" and isinstance(payload.get("message"), str):
        return payload["message"]
    return None


def is_transient_payload(payload: Any, status: Optional[int] = None) -> bool:
    """True when an upstream JSON body describes a transient failure."""
    text = find_transient_payload(payload)
    if text is None:
        return False
    return is_transient_error(text, status)


def iter_transient_sse_lines(lines: Iterable[str]) -> Optional[str]:
    """Return the first SSE data line that carries a transient error.

    The upstream can answer HTTP 200 and then deliver the rejection inside
    the stream (``data: {"error": {...}}``). A normal upstream event never
    has an ``error`` key, so the first such line is the failure.
    """
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if "error" not in payload and payload.get("type") != "error":
            continue
        text = find_transient_payload(payload) or body
        if is_transient_error(text):
            return text
    return None


def find_sse_error_text(raw: bytes) -> Optional[str]:
    """Return the error text of the first SSE event in ``raw`` that carries one.

    Only complete events are inspected (a trailing partial event is not an
    error). Returns ``None`` when the bytes hold no error — the caller then
    decides whether the error it found is transient.
    """
    head = raw.decode("utf-8", "replace")
    for block in head.split("\n\n"):
        if not block.strip():
            continue
        for line in block.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                payload = json.loads(body)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if "error" not in payload and payload.get("type") != "error":
                continue
            return find_transient_payload(payload) or body
    return None


def sse_error_status(data: str) -> Optional[int]:
    """HTTP status for a transient error delivered inside an SSE stream.

    Returns ``429`` for a rate-limit rejection and ``503`` for any other
    transient failure, or ``None`` when the data is not a transient error
    (a normal upstream event, or a permanent failure).
    """
    try:
        payload = json.loads(data)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if "error" not in payload and payload.get("type") != "error":
        return None
    text = find_transient_payload(payload) or data
    if not is_transient_error(text):
        return None
    return 429 if is_rate_limited(text) else 503


__all__ = [
    "Retrier",
    "RetryExhausted",
    "RetryPolicy",
    "TransientUpstreamError",
    "find_sse_error_text",
    "find_transient_payload",
    "is_rate_limited",
    "is_transient_error",
    "is_transient_exception",
    "is_transient_payload",
    "is_transient_status",
    "iter_transient_sse_lines",
    "retry_settings",
    "sse_error_status",
    "status_from_exception",
]
