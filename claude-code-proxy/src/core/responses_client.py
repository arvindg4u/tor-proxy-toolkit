"""Async client for the OpenAI Responses API (used on the responses wire path)."""

import asyncio
import json
import logging
from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from fastapi import HTTPException

from src.core.config import config
from src.core.guards import scrub_api_keys
from src.core.http_client import get_shared_client, get_stream_timeout

logger = logging.getLogger(__name__)


def classify_responses_error(error_detail: Any) -> str:
    """Provide specific guidance for common Responses API failures."""
    error_str = str(error_detail).lower()

    if "freelimit" in error_str or "free usage" in error_str or "429" in error_str:
        return (
            "Free-tier rate limit exceeded. Wait before retrying, or check the "
            "upstream User-Agent allowlist (bot UAs get a tiny quota)."
        )
    if "missingsession" in error_str or "only be used in opencode" in error_str:
        return (
            "Upstream rejected the request as non-OpenCode traffic "
            "(MissingSessionID). The proxy sends x-opencode-session "
            "automatically — restart it to pick up the fix, or set "
            "OPENCODE_SESSION_ID explicitly."
        )
    if "credits" in error_str or "payment" in error_str or "billing" in error_str:
        return (
            "Upstream reports missing credits/payment method. Use a *-free "
            "model or add a payment method to the upstream workspace."
        )
    if "unauthorized" in error_str or "invalid_api_key" in error_str or " 401" in error_str:
        return "Invalid API key. Please check your OPENAI_API_KEY configuration."
    if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
        return "Model not found. Please check your BIG_MODEL/MIDDLE_MODEL/SMALL_MODEL configuration."
    if "unavailable" in error_str:
        return "Model is temporarily unavailable upstream. Retry or pick another free model."
    return str(error_detail)


def _extract_sse_events(buffer: bytearray) -> "list[str]":
    """Pop complete SSE events (blank-line delimited) off a byte buffer.

    Phase 3: split framing on raw bytes (``b"\\n\\n"`` boundaries are ASCII,
    so a split can never cut a multi-byte UTF-8 sequence) and decode one
    complete event at a time. A trailing partial event stays buffered.
    CRLF is normalized on the accumulated buffer, so a ``\\r\\n`` split
    across two TCP chunks still frames. Returns decoded, non-blank events.
    """
    if b"\r" in buffer:
        # Hold back a trailing CR: it may be the first half of a CRLF
        # split across two TCP chunks (converting it now would plant a
        # phantom blank line when the LF arrives). The end-of-stream flush
        # handles a genuinely trailing CR.
        trailing_cr = buffer.endswith(b"\r")
        if trailing_cr:
            del buffer[-1:]
        buffer[:] = buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if trailing_cr:
            buffer += b"\r"
    events = []
    while True:
        idx = buffer.find(b"\n\n")
        if idx < 0:
            break
        raw = bytes(buffer[:idx])
        del buffer[: idx + 2]
        text = raw.decode("utf-8", "replace")
        if text.strip():
            events.append(text)
    return events


def _parse_sse_event(text: str):
    """Split one raw SSE event text into (event_type, data_str|None)."""
    event_type: Optional[str] = None
    data_lines: "list[str]" = []
    for line in text.split("\n"):
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        # ignore ":" comments and other fields
    if event_type is None and not data_lines:
        return "message", None
    return event_type or "message", "\n".join(data_lines)


class ResponsesClient:
    """Async Responses API client with cancellation support."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 90,
        user_agent: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        retry_budget_secs: float = 120,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": user_agent or "opencode/1.18.18",
        }
        if custom_headers:
            self.headers.update(custom_headers)
        self.retry_budget_secs = retry_budget_secs
        self.active_requests: Dict[str, asyncio.Event] = {}

    def _url(self) -> str:
        return f"{self.base_url}/responses"

    def _request_headers(self) -> Dict[str, str]:
        """Per-call headers: fresh CLI identity (msg id) over the base set."""
        return {**self.headers, **config.get_upstream_headers()}

    @staticmethod
    def _dump_failure(
        status: int,
        error: str,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        ttl: Optional[str] = None,
    ) -> None:
        """Save a compact failure record locally for diagnosis.

        Stores the upstream error text + timestamp first; only a truncated
        payload summary (never the full prompt) so the dashboard shows the
        actual error instead of a wall of request text.
        """
        import os
        import time as _time

        try:
            log_dir = os.environ.get("PROXY_LOG_DIR", "logs")
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "last_upstream_failure.json")
            with open(path, "w") as f:
                json.dump(
                    {
                        "status": status,
                        "error": scrub_api_keys(error or "")[:2000],
                        "model": model,
                        "request_id": request_id,
                        "cache_ttl": ttl,
                        "at": _time.time(),
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass

    @staticmethod
    def _retryable(exc: HTTPException) -> bool:
        """Only server-side 5xx are worth retrying (never 4xx/499)."""
        return exc.status_code is not None and exc.status_code >= 500

    async def _backoff(self, attempt: int, deadline: float) -> bool:
        """Sleep with exponential backoff; False when the budget is spent."""
        import time

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(2**attempt, 30, remaining))
        return True

    async def create_response(
        self, payload: Dict[str, Any], request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST a Responses request and return the decoded JSON object."""
        if request_id:
            self.active_requests[request_id] = asyncio.Event()
        try:
            import time

            deadline = time.monotonic() + self.retry_budget_secs
            attempt = 0
            while True:
                try:
                    client = await get_shared_client()
                    resp = await client.post(
                        self._url(),
                        json=payload,
                        headers=self._request_headers(),
                        timeout=self.timeout,
                    )
                    if resp.status_code >= 400:
                        err_text = resp.text
                        self._dump_failure(
                            resp.status_code,
                            err_text,
                            model=payload.get("model"),
                            request_id=request_id,
                        )
                        raise HTTPException(
                            status_code=resp.status_code,
                            detail=classify_responses_error(err_text),
                        )
                    break
                except HTTPException as e:
                    if self._retryable(e) and await self._backoff(attempt, deadline):
                        attempt += 1
                        logger.warning(
                            "Upstream %s (model=%s), retrying in budget (attempt %d)",
                            e.status_code,
                            payload.get("model"),
                            attempt,
                        )
                        continue
                    raise
            data = resp.json()
            if isinstance(data, dict) and data.get("type") == "error":
                err_text = json.dumps(data)
                detail = classify_responses_error(err_text)
                lowered = err_text.lower()
                if (
                    "freelimit" in lowered
                    or "free usage" in lowered
                    or "429" in lowered
                    or "rate limit" in lowered
                ):
                    # Upstream HTTP 200 carrying a rate-limit error body
                    # (e.g. FreeUsageLimitError): signal it like an HTTP 429
                    # so the failure watcher rotates the egress peer.
                    self._dump_failure(
                        429, err_text, model=payload.get("model"), request_id=request_id
                    )
                    raise HTTPException(status_code=429, detail=detail)
                self._dump_failure(
                    500, err_text, model=payload.get("model"), request_id=request_id
                )
                raise HTTPException(
                    status_code=500,
                    detail=detail,
                )
            return data
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Unexpected error: {str(e)}"
            )
        finally:
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    async def create_response_stream(
        self, payload: Dict[str, Any], request_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """POST a streaming Responses request, yielding one SSE event per item.

        Each yielded string has the form ``"event: <type>\\ndata: <json>"``.

        The upstream POST + status check happen lazily on first iteration.
        Callers that need an upstream rejection *before* their own response
        headers go out can prime via :func:`prime_response_stream`; the
        /v1/messages path intentionally does not (Phase 2 optimistic start)
        and maps post-header rejections to SSE ``error`` events instead.
        """
        if request_id:
            self.active_requests[request_id] = asyncio.Event()
        try:
            body = dict(payload)
            body["stream"] = True
            import time

            deadline = time.monotonic() + self.retry_budget_secs
            attempt = 0
            yielded_any = False
            while True:
                try:
                    client = await get_shared_client()
                    async with client.stream(
                        "POST",
                        self._url(),
                        json=body,
                        headers=self._request_headers(),
                        timeout=get_stream_timeout(),
                    ) as resp:
                            if resp.status_code >= 400:
                                err_body = await resp.aread()
                                err_text = err_body.decode("utf-8", "replace")
                                self._dump_failure(
                                    resp.status_code,
                                    err_text,
                                    model=body.get("model"),
                                    request_id=request_id,
                                )
                                raise HTTPException(
                                    status_code=resp.status_code,
                                    detail=classify_responses_error(err_text),
                                )
                            # Phase 3: frame on raw bytes (aiter_bytes + blank-line
                            # split) instead of aiter_lines: fewer per-line
                            # Python ops and one decode per event. Cancellation
                            # is checked per TCP chunk rather than per line.
                            buf = bytearray()
                            async for chunk in resp.aiter_bytes():
                                if request_id and self.active_requests.get(request_id) is not None:
                                    if self.active_requests[request_id].is_set():
                                        raise HTTPException(
                                            status_code=499,
                                            detail="Request cancelled by client",
                                        )
                                if not chunk:
                                    continue
                                buf += chunk
                                if len(buf) > 1_000_000:
                                    # A single SSE event without a blank-line
                                    # boundary: pathological, don't OOM.
                                    raise HTTPException(
                                        status_code=500,
                                        detail="Upstream SSE event exceeded 1MB",
                                    )
                                for text in _extract_sse_events(buf):
                                    event_type, data = _parse_sse_event(text)
                                    if data is None:
                                        continue
                                    yielded_any = True
                                    yield f"event: {event_type}\ndata: {data}"
                            tail = bytes(buf).decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
                            if tail.strip():
                                event_type, data = _parse_sse_event(tail)
                                if data is not None:
                                    yielded_any = True
                                    yield f"event: {event_type}\ndata: {data}"
                    break
                except HTTPException as e:
                    # Retryable only before any bytes were yielded (safe:
                    # nothing sent downstream yet) and within budget.
                    if (
                        not yielded_any
                        and self._retryable(e)
                        and await self._backoff(attempt, deadline)
                    ):
                        attempt += 1
                        logger.warning(
                            "Upstream stream %s (model=%s), retrying in budget (attempt %d)",
                            e.status_code,
                            body.get("model"),
                            attempt,
                        )
                        continue
                    raise
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Unexpected error: {str(e)}"
            )
        finally:
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request by request_id."""
        if request_id in self.active_requests:
            self.active_requests[request_id].set()
            return True
        return False


async def prime_response_stream(async_gen):
    """Pull the first event, returning a chained generator.

    Forces the upstream POST + status check to happen now, so an
    HTTPException for an upstream rejection is raised before the caller
    starts its own HTTP response. The first event is replayed, so no data
    is lost.
    """
    first = await async_gen.__anext__()

    async def chained():
        yield first
        async for event in async_gen:
            yield event

    return chained()
