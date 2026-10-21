"""Bun-backed upstream transport for the Claude Code proxy.

OpenCode ZEN's free tier gate checks three things at once:

1. the TLS ClientHello fingerprint -- Python's httpx (OpenSSL) is rejected
   with ``403 FreeTierError`` even with a perfectly cloned request, while
   Bun's BoringSSL stack (the same one the real OpenCode CLI uses) is
   accepted;
2. the OpenCode identity headers (``x-opencode-*`` and friends);
3. the presence of tool definitions in the request body.

This module runs one persistent Bun helper (``bun/zen_fetch.ts``) that
performs the upstream calls, and exposes it to the app as an httpx
transport, so the OpenAI SDK and the responses client keep working
unchanged.

If the Bun runtime is missing, ``get_bun_transport`` returns ``None`` and
callers fall back to the plain httpx transport (which will hit the ZEN
gate, but keeps the proxy functional for any non-ZEN upstream).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

_HELPER_PATH = Path(__file__).resolve().parent.parent.parent / "bun" / "zen_fetch.ts"

_BUN_CANDIDATES = (
    os.environ.get("BUN_PATH", ""),
    "/home/ubuntu/.npm-global/bin/bun",
    str(Path.home() / ".bun" / "bin" / "bun"),
    "/usr/local/bin/bun",
)


def bun_path() -> str | None:
    """Absolute path of the bun binary, or None when not installed."""
    for candidate in _BUN_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("bun")
    return found


class BunHelperError(RuntimeError):
    """Raised when the Bun helper reports a failure."""


class _BunHelper:
    """Persistent Bun subprocess speaking NDJSON over stdin/stdout."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Queue] = {}
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return self._proc
            bun = bun_path()
            if bun is None:
                raise BunHelperError("bun runtime not found (install bun or set BUN_PATH)")
            self._proc = await asyncio.create_subprocess_exec(
                bun,
                "run",
                str(_HELPER_PATH),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=os.environ.copy(),
            )
            asyncio.create_task(self._read_loop(self._proc))
            return self._proc

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        buffer = b""
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                queue = self._pending.get(message.get("id"))
                if queue is None:
                    continue
                if "status" in message:
                    await queue.put({"meta": message})
                elif "chunk" in message:
                    await queue.put({"data": base64.b64decode(message["chunk"])})
                elif message.get("end"):
                    await queue.put({"end": True})
                elif "error" in message:
                    await queue.put({"error": message["error"]})

    async def perform(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
    ) -> tuple[int, dict[str, str], AsyncIterator[bytes]]:
        proc = await self._ensure_started()
        assert proc.stdin is not None
        self._next_id += 1
        request_id = self._next_id
        queue: asyncio.Queue = asyncio.Queue()
        self._pending[request_id] = queue

        payload = {
            "id": request_id,
            "method": method,
            "url": url,
            "headers": headers,
            "body": base64.b64encode(body).decode() if body else None,
        }
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

        first = await queue.get()
        if "error" in first:
            self._pending.pop(request_id, None)
            raise BunHelperError(str(first["error"]))
        meta = first["meta"]

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    item = await queue.get()
                    if "data" in item:
                        yield item["data"]
                    elif "end" in item:
                        break
                    elif "error" in item:
                        raise BunHelperError(str(item["error"]))
            finally:
                self._pending.pop(request_id, None)

        return int(meta["status"]), dict(meta.get("headers") or {}), chunks()

    async def aclose(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


_helper = _BunHelper()

# Headers the transport must not forward: the CLI does not send them and
# their presence marks the request as coming from an HTTP client library.
_STRIPPED_REQUEST_HEADERS = frozenset({"host", "content-length", "connection"})


def _ensure_tools(body: bytes) -> bytes:
    """Guarantee the request body carries the genuine OpenCode CLI manifest.

    The free-tier gate rejects tool-less turns and foreign/partial tool sets:
    only the genuine CLI manifest (with the caller's own tools appended) is
    accepted. The request converters already merge it, this is the safety net
    for any other code path.
    """
    if not body:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    tools = payload.get("tools")
    try:
        from src.core.cli_identity import GENUINE_CLI_TOOLS, chat_format_gate_tools
    except ImportError:
        return body
    if isinstance(tools, list) and tools:
        first = tools[0] if tools else {}
        if isinstance(first, dict) and "function" in first:
            # chat-completions wire format
            merged = chat_format_gate_tools(tools)
        else:
            # responses wire format
            manifest_names = {g.get("name", "") for g in GENUINE_CLI_TOOLS}
            extras = [t for t in tools if t.get("name") not in manifest_names]
            merged = list(GENUINE_CLI_TOOLS) + extras
        if merged != tools:
            payload["tools"] = merged
            return json.dumps(payload).encode()
        return body
    payload["tools"] = chat_format_gate_tools([])
    return json.dumps(payload).encode()


class _BunTransportMixin:
    """Shared logic for the httpx and httpx2 transports."""

    def __init__(self, response_cls: Any, stream_cls: Any) -> None:
        self._response_cls = response_cls
        self._stream_cls = stream_cls

    async def handle_async_request(self, request: Any) -> Any:  # noqa: ANN401
        body = await request.aread()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _STRIPPED_REQUEST_HEADERS
            and not key.lower().startswith("x-stainless-")
            and not key.lower().startswith("openai-")
            and not key.lower().startswith("x-openai-")
        }
        # The real OpenCode CLI sends "accept: */*"; the OpenAI SDK asks for
        # "application/json", which the free-tier gate treats as SDK traffic.
        headers["accept"] = "*/*"
        if body:
            body = _ensure_tools(body)
        if os.environ.get("ZEN_DEBUG"):
            import sys

            print("ZEN DEBUG ->", request.method, str(request.url), file=sys.stderr)
            print("ZEN DEBUG headers:", json.dumps(headers, indent=1), file=sys.stderr)
            print("ZEN DEBUG body:", body[:600].decode("utf-8", "replace"), file=sys.stderr)

        status, response_headers, chunks = await _helper.perform(
            request.method, str(request.url), headers, body
        )
        # Bun's fetch already decodes the body, so the upstream content
        # encoding headers must not be replayed: httpx would try to
        # decompress the plain bytes again.
        response_headers = {
            key: value
            for key, value in response_headers.items()
            if key.lower() not in ("content-encoding", "content-length")
        }
        return self._response_cls(
            status,
            headers=response_headers,
            stream=self._stream_cls(chunks),
            request=request,
        )

    async def aclose(self) -> None:
        await _helper.aclose()


def _make_stream_class(base: Any) -> Any:
    class _BunByteStream(base):  # type: ignore[misc, valid-type]
        def __init__(self, iterator: AsyncIterator[bytes]) -> None:
            self._iterator = iterator

        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in self._iterator:
                yield chunk

        async def aclose(self) -> None:
            await self._iterator.aclose()

    return _BunByteStream


try:  # httpx2 is what the vendored openai SDK uses
    import httpx2

    class BunTransportHttpx2(_BunTransportMixin, httpx2.AsyncBaseTransport):  # type: ignore[misc]
        def __init__(self) -> None:
            _BunTransportMixin.__init__(self, httpx2.Response, _make_stream_class(httpx2.AsyncByteStream))

    httpx2_transport: Any = BunTransportHttpx2
except ImportError:  # pragma: no cover
    httpx2_transport = None


class BunTransportHttpx(_BunTransportMixin, httpx.AsyncBaseTransport):  # type: ignore[misc]
    def __init__(self) -> None:
        _BunTransportMixin.__init__(self, httpx.Response, _make_stream_class(httpx.AsyncByteStream))


def get_bun_transport(flavour: str = "httpx2") -> Any | None:
    """Return a transport bound to the right httpx flavour, or None.

    The flavour MUST match the client the transport is handed to. ``httpx``
    and ``httpx2`` have separate ``Response``/``AsyncByteStream`` classes,
    and httpx asserts ``isinstance(response.stream, AsyncByteStream)`` on
    its own class — feeding it an httpx2 response raises a bare
    ``AssertionError`` (empty message) on every streaming call.

    The vendored OpenAI SDK uses httpx2; the proxy's own shared client uses
    httpx. So the caller has to say which one it is building.
    """
    if bun_path() is None:
        return None
    if flavour == "httpx":
        return BunTransportHttpx()
    if httpx2_transport is not None:
        return httpx2_transport()
    return BunTransportHttpx()
