"""Shared httpx.AsyncClient singleton with connection pooling.

Phase 1 quick win: per-request ``httpx.AsyncClient`` construction forfeits
TCP/TLS keepalive. A single long-lived client reuses connections, saving a
handshake on every upstream call.

Streaming calls use split timeouts (no read timeout + keepalive pings keep
long reasoning pauses alive); non-streaming calls keep the configured
REQUEST_TIMEOUT.
"""

import os
import threading

import httpx

_shared_client: httpx.AsyncClient | None = None
# Plain threading guard: client construction is synchronous (no awaits
# inside the critical section), so this is safe on any event loop.
_client_guard = threading.Lock()

_DEFAULT_TIMEOUT = 90.0


def _request_timeout_secs() -> float:
    try:
        return float(os.environ.get("REQUEST_TIMEOUT", str(_DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT


def _http2_supported() -> bool:
    """Whether to negotiate HTTP/2 upstream.

    Env HTTP2 forces the choice ("1"/"0"); otherwise autodetect via h2.
    """
    forced = os.environ.get("HTTP2")
    if forced == "1":
        return True
    if forced == "0":
        return False
    try:
        import h2  # noqa: F401

        return True
    except ImportError:
        return False


def get_stream_timeout() -> httpx.Timeout:
    """Timeout for streaming upstream calls.

    connect/pool bounded, write bounded, read disabled: a healthy stream
    may go silent for minutes during reasoning while keepalive pings flow.
    """
    return httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)


async def get_shared_client() -> httpx.AsyncClient:
    """Return the process-wide shared AsyncClient (created on first use)."""
    global _shared_client
    with _client_guard:
        if _shared_client is not None and not _shared_client.is_closed:
            return _shared_client
        limits = httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )
        _shared_client = httpx.AsyncClient(
            timeout=_request_timeout_secs(),
            limits=limits,
            http2=_http2_supported(),
        )
        return _shared_client


async def aclose_shared_client() -> None:
    """Close the shared client (called on app shutdown)."""
    global _shared_client
    client = None
    with _client_guard:
        if _shared_client is not None:
            client, _shared_client = _shared_client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:
            pass
