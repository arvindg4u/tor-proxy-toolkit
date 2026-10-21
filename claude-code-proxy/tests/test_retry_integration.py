"""End-to-end retry tests: the proxy must absorb transient upstream failures.

These drive the real client / passthrough code with a fake upstream, so they
cover the behaviour the operator actually asked for: a flapping free-tier
model is retried inside the proxy instead of ending the client's turn with
"Model is temporarily unavailable upstream".

Runs under pytest when available, and also as a plain script:
    python3 tests/test_retry_integration.py
(no third-party test deps required).
"""

import asyncio
import json
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import HTTPException  # noqa: E402

from src.core.client import OpenAIClient  # noqa: E402
from src.core.config import config  # noqa: E402
from src.core.retry import RetryPolicy  # noqa: E402

# The retry policy is built from the environment when the config module is
# imported, so the tests would otherwise wait the real 10s between attempts.
# Patch it to a zero-interval policy for the duration of the test run.
config.retry_policy = RetryPolicy(attempts=10, interval=0)


# --- chat streaming path --------------------------------------------------

class FakeStream:
    """Minimal stand-in for the OpenAI SDK's async stream."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class FakeChunk:
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


class FakeCompletions:
    """Records every attempt and replays a scripted failure sequence."""

    def __init__(self, failures, success_chunks):
        self.failures = list(failures)
        self.success_chunks = success_chunks
        self.attempts = 0

    async def create(self, **request):
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)
        return FakeStream(self.success_chunks)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeSDKClient:
    def __init__(self, completions):
        self.chat = FakeChat(completions)


def _client(completions):
    client = OpenAIClient("sk-test", "https://example.invalid/v1")
    client.client = FakeSDKClient(completions)
    return client


def _api_error(message, status=None):
    """An APIError-alike carrying a status code, like the SDK raises."""
    from openai._exceptions import APIError

    err = APIError(message=message, request=None, body=None)
    if status is not None:
        err.status_code = status
    return err


def test_chat_stream_retries_transient_failure():
    """A transient upstream error is retried, and the stream still delivers."""
    completions = FakeCompletions(
        failures=[
            _api_error("Model is temporarily unavailable upstream", 503),
            _api_error("Model is temporarily unavailable upstream", 503),
        ],
        success_chunks=[FakeChunk({"id": "c1", "choices": []})],
    )
    client = _client(completions)

    async def run():
        gen = client.create_chat_completion_stream({"model": "m", "stream": True})
        items = [item async for item in gen]
        return items

    items = asyncio.run(run())
    assert completions.attempts == 3, completions.attempts
    # A keepalive tick was emitted for each wait, and the real chunk came
    # through afterwards.
    kinds = [item.get("type") for item in items]
    assert kinds.count("ping") == 2, kinds
    assert kinds[-1] == "done", kinds
    assert any(item.get("type") == "chunk" for item in items), kinds


def test_chat_stream_does_not_retry_permanent_failure():
    """A 400 is the caller's fault: it must surface immediately."""
    completions = FakeCompletions(
        failures=[_api_error("invalid request", 400)],
        success_chunks=[],
    )
    client = _client(completions)

    async def run():
        gen = client.create_chat_completion_stream({"model": "m", "stream": True})
        return [item async for item in gen]

    try:
        asyncio.run(run())
    except HTTPException as e:
        assert e.status_code == 400, e.status_code
    else:
        raise AssertionError("expected the 400 to surface")
    assert completions.attempts == 1, completions.attempts


def test_chat_stream_gives_up_after_the_attempt_budget():
    """After the cap, the error surfaces instead of retrying forever."""
    failures = [
        _api_error("Model is temporarily unavailable upstream", 503)
        for _ in range(20)
    ]
    completions = FakeCompletions(failures=failures, success_chunks=[])
    client = _client(completions)

    async def run():
        gen = client.create_chat_completion_stream({"model": "m", "stream": True})
        return [item async for item in gen]

    try:
        asyncio.run(run())
    except HTTPException as e:
        assert e.status_code == 503, e.status_code
    else:
        raise AssertionError("expected the 503 to surface")
    assert completions.attempts == 10, completions.attempts


# --- passthrough path -----------------------------------------------------

def test_passthrough_retries_transient_status():
    """A 5xx from the upstream is retried before it reaches the client."""
    from src.api import endpoints

    attempts = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeHTTPClient:
        async def post(self, url, content=None, headers=None, params=None, timeout=None):
            attempts.append(url)
            if len(attempts) < 3:
                return FakeResp(503, {"error": {"message": "Model is temporarily unavailable upstream"}})
            return FakeResp(200, {"choices": [{"message": {"content": "hi"}}]})

    async def fake_get_shared_client():
        return FakeHTTPClient()

    original = endpoints.get_shared_client
    endpoints.get_shared_client = fake_get_shared_client
    try:
        resp = asyncio.run(
            endpoints._post_upstream_with_retry(
                "https://example.invalid/v1/chat/completions",
                b"{}",
                {"Authorization": "Bearer sk-test"},
                {},
                "/v1/chat/completions",
                {"model": "m"},
            )
        )
    finally:
        endpoints.get_shared_client = original

    assert len(attempts) == 3, attempts
    assert resp.status_code == 200, resp.status_code


def test_passthrough_does_not_retry_permanent_status():
    """A 401 must reach the client immediately."""
    from src.api import endpoints

    attempts = []

    class FakeResp:
        status_code = 401
        text = '{"error": {"message": "invalid api key"}}'

        def json(self):
            return {"error": {"message": "invalid api key"}}

    class FakeHTTPClient:
        async def post(self, url, content=None, headers=None, params=None, timeout=None):
            attempts.append(url)
            return FakeResp()

    async def fake_get_shared_client():
        return FakeHTTPClient()

    original = endpoints.get_shared_client
    endpoints.get_shared_client = fake_get_shared_client
    try:
        resp = asyncio.run(
            endpoints._post_upstream_with_retry(
                "https://example.invalid/v1/chat/completions",
                b"{}",
                {"Authorization": "Bearer sk-test"},
                {},
                "/v1/chat/completions",
                {"model": "m"},
            )
        )
    finally:
        endpoints.get_shared_client = original

    assert len(attempts) == 1, attempts
    assert resp.status_code == 401, resp.status_code


def test_passthrough_retries_rate_limit_body_on_http_200():
    """The free tier's rate-limit rejection arrives as a 200 + error body."""
    from src.api import endpoints

    attempts = []

    class FakeResp:
        def __init__(self, status, payload):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class FakeHTTPClient:
        async def post(self, url, content=None, headers=None, params=None, timeout=None):
            attempts.append(url)
            if len(attempts) < 2:
                return FakeResp(
                    200,
                    {
                        "error": {
                            "type": "FreeUsageLimitError",
                            "message": "Rate limit exceeded. Please try again later.",
                        }
                    },
                )
            return FakeResp(200, {"choices": [{"message": {"content": "hi"}}]})

    async def fake_get_shared_client():
        return FakeHTTPClient()

    original = endpoints.get_shared_client
    endpoints.get_shared_client = fake_get_shared_client
    try:
        resp = asyncio.run(
            endpoints._post_upstream_with_retry(
                "https://example.invalid/v1/chat/completions",
                b"{}",
                {"Authorization": "Bearer sk-test"},
                {},
                "/v1/chat/completions",
                {"model": "m"},
            )
        )
    finally:
        endpoints.get_shared_client = original

    assert len(attempts) == 2, attempts
    assert resp.status_code == 200, resp.status_code


# --- ping forwarding ------------------------------------------------------

def test_ping_item_becomes_an_sse_ping_event():
    """The keepalive tick reaches the client as an SSE ping event."""
    from src.conversion.response_converter import (
        _parse_chat_item,
        convert_openai_streaming_to_claude_with_cancellation,
    )

    class FakeRequest:
        async def is_disconnected(self):
            return False

    class FakeClient:
        def cancel_request(self, request_id):
            return True

    class FakeOriginal:
        model = "test-model"

    async def run():
        async def stream():
            yield {"type": "ping"}
            yield {"type": "chunk", "chunk": {"choices": []}}
            yield {"type": "done"}

        events = []
        async for event in convert_openai_streaming_to_claude_with_cancellation(
            stream(), FakeOriginal(), None, FakeRequest(), FakeClient(), "rid"
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    pings = [e for e in events if e.startswith("event: ping")]
    # One opening ping plus the retry tick's ping.
    assert len(pings) == 2, events


def test_parse_chat_item_recognises_ping():
    from src.conversion.response_converter import _parse_chat_item

    assert _parse_chat_item({"type": "ping"}) == ("ping", None)


def test_shared_client_uses_a_matching_bun_transport():
    """The shared client is httpx, so its transport must be the httpx flavour.

    httpx and httpx2 have separate Response/AsyncByteStream classes, and
    httpx asserts `isinstance(response.stream, AsyncByteStream)` on its
    OWN class. Handing the httpx client an httpx2 transport makes every
    streaming call die with a bare AssertionError (empty message) — which
    is what broke the passthrough streaming path.
    """
    import httpx

    import httpx2 as _httpx2

    from src.core.bun_transport import BunTransportHttpx, get_bun_transport

    transport = get_bun_transport(flavour="httpx")
    if transport is None:
        return  # Bun runtime not installed: nothing to assert
    assert isinstance(transport, BunTransportHttpx), type(transport)
    # The response class it builds must belong to the httpx flavour the
    # shared client uses.
    assert issubclass(transport._response_cls, httpx.Response)
    assert not issubclass(transport._response_cls, _httpx2.Response)


def test_passthrough_stream_retries_an_error_delivered_inside_the_stream():
    """A 200 response can still carry the rejection inside the stream.

    The upstream sometimes answers 200 and only then emits an error event.
    Forwarding that to the client would end its turn, so the proxy holds
    the first event back, detects the error, and retries — the client must
    only ever see the good data.
    """
    from src.api import endpoints
    from src.core.retry import RetryPolicy

    error_body = b'data: {"error": {"type": "FreeUsageLimitError", "message": "Rate limit exceeded."}}\n\n'
    good_body = b'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'

    class FakeResp:
        def __init__(self, chunks):
            self.status_code = 200
            self._chunks = chunks

        async def aread(self):
            return b"".join(self._chunks)

        def aiter_bytes(self):
            async def gen():
                for c in self._chunks:
                    yield c

            return gen()

    class FakeStreamCtx:
        def __init__(self, resp):
            self._r = resp

        async def __aenter__(self):
            return self._r

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, responses):
            self._responses = responses
            self.calls = 0

        def stream(self, *a, **k):
            self.calls += 1
            idx = min(self.calls - 1, len(self._responses) - 1)
            return FakeStreamCtx(self._responses[idx])

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def run():
        config.retry_policy = RetryPolicy(attempts=3, interval=0)
        client = FakeClient([FakeResp([error_body]), FakeResp([good_body])])

        async def fake_get_shared_client():
            return client

        original = endpoints.get_shared_client
        endpoints.get_shared_client = fake_get_shared_client
        try:
            out = []
            async for chunk in endpoints._passthrough_stream_gen(
                FakeRequest(), "https://example.invalid/v1/chat/completions",
                b"{}", {}, {},
            ):
                out.append(chunk)
            return out, client.calls
        finally:
            endpoints.get_shared_client = original

    out, calls = asyncio.run(run())
    assert calls == 2, calls
    joined = b"".join(out)
    assert b"Rate limit" not in joined, joined
    assert b"hi" in joined, joined


def test_passthrough_stream_forwards_a_trailing_partial_event():
    """A stream whose last event has no blank-line boundary is not dropped.

    The first event is held back until it is known to be good, so the
    trailing partial event has to be flushed when the stream ends —
    otherwise the client's last event silently disappears.
    """
    from src.api import endpoints
    from src.core.retry import RetryPolicy

    body = b'data: {"choices": [{"delta": {"content": "hi"}}]}'  # no \n\n

    class FakeResp:
        status_code = 200

        async def aread(self):
            return body

        def aiter_bytes(self):
            async def gen():
                yield body

            return gen()

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResp()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def stream(self, *a, **k):
            return FakeStreamCtx()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def run():
        config.retry_policy = RetryPolicy(attempts=3, interval=0)

        async def fake_get_shared_client():
            return FakeClient()

        original = endpoints.get_shared_client
        endpoints.get_shared_client = fake_get_shared_client
        try:
            out = []
            async for chunk in endpoints._passthrough_stream_gen(
                FakeRequest(), "https://example.invalid/v1/chat/completions",
                b"{}", {}, {},
            ):
                out.append(chunk)
            return out
        finally:
            endpoints.get_shared_client = original

    out = asyncio.run(run())
    assert b"hi" in b"".join(out), out


_TESTS = [
    test_chat_stream_retries_transient_failure,
    test_chat_stream_does_not_retry_permanent_failure,
    test_chat_stream_gives_up_after_the_attempt_budget,
    test_passthrough_retries_transient_status,
    test_passthrough_does_not_retry_permanent_status,
    test_passthrough_retries_rate_limit_body_on_http_200,
    test_passthrough_stream_retries_an_error_delivered_inside_the_stream,
    test_passthrough_stream_forwards_a_trailing_partial_event,
    test_ping_item_becomes_an_sse_ping_event,
    test_parse_chat_item_recognises_ping,
    test_shared_client_uses_a_matching_bun_transport,
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
