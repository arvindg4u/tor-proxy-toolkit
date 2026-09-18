from fastapi import APIRouter, HTTPException, Request, Header, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from datetime import datetime
import asyncio
import os
import time
import uuid
from typing import Optional

from src.core.config import config
from src.core.guards import body_too_large, sanitize_passthrough_path, scrub_api_keys
from src.core.http_client import get_shared_client, get_stream_timeout
from src.core.logging import logger
from src.core.client import OpenAIClient
from src.core.responses_client import ResponsesClient
from src.core.stats import stats
from src.api.dashboard import dashboard_response
from src.models.claude import ClaudeMessagesRequest, ClaudeTokenCountRequest
from src.conversion.request_converter import convert_claude_to_openai
from src.conversion.request_responses import convert_claude_to_responses
from src.conversion.response_converter import (
    convert_openai_to_claude_response,
    convert_openai_streaming_to_claude_with_cancellation,
)
from src.conversion.response_responses import (
    _split_event,
    convert_responses_to_claude_response,
    convert_responses_streaming_to_claude_with_cancellation,
)
from src.core.model_manager import model_manager

router = APIRouter()

# Max request bodies (Content-Length guard; chunked bodies bypass the check
# but this is a localhost proxy — the guard stops trivial OOMs).
MAX_MESSAGES_BODY_BYTES = 20_000_000
MAX_PASSTHROUGH_BODY_BYTES = 20_000_000
MAX_COUNT_TOKENS_BODY_BYTES = 1_000_000

# Get custom headers from config, layered over the OpenCode identity
# headers (session/UA) that ZEN's free tier requires.
custom_headers = {**config.get_upstream_headers(), **config.get_custom_headers()}

openai_client = OpenAIClient(
    config.openai_api_key,
    config.openai_base_url,
    config.request_timeout,
    api_version=config.azure_api_version,
    custom_headers=custom_headers,
)

responses_client = ResponsesClient(
    config.openai_api_key,
    config.openai_base_url,
    config.request_timeout,
    user_agent=config.upstream_user_agent,
    custom_headers=custom_headers,
    retry_budget_secs=config.responses_retry_budget_secs,
)

async def validate_api_key(x_api_key: Optional[str] = Header(None), authorization: Optional[str] = Header(None)):
    """Validate the client's API key from either x-api-key header or Authorization header."""
    client_api_key = None
    
    # Extract API key from headers
    if x_api_key:
        client_api_key = x_api_key
    elif authorization and authorization.startswith("Bearer "):
        client_api_key = authorization[len("Bearer "):]
    
    # Skip validation if ANTHROPIC_API_KEY is not set in the environment
    if not config.anthropic_api_key:
        return
        
    # Validate the client API key
    if not client_api_key or not config.validate_client_api_key(client_api_key):
        logger.warning(f"Invalid API key provided by client")
        raise HTTPException(
            status_code=401,
            detail="Invalid API key. Please provide a valid Anthropic API key."
        )

@router.post("/v1/messages")
async def create_message(request: ClaudeMessagesRequest, http_request: Request, _: None = Depends(validate_api_key)):
    t0 = time.monotonic()
    if body_too_large(http_request.headers, MAX_MESSAGES_BODY_BYTES):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        logger.debug(
            f"Processing Claude request: model={request.model}, stream={request.stream}"
        )

        # Generate unique request ID for cancellation tracking
        request_id = str(uuid.uuid4())

        # Responses wire path (for responses-only upstreams such as Muse
        # Spark on OpenCode ZEN): Claude -> Responses -> Claude.
        if config.upstream_wire_api == "responses":
            return await _handle_responses_message(
                request, http_request, request_id, t0
            )

        # Convert Claude request to OpenAI format
        openai_request = convert_claude_to_openai(request, model_manager)
        stats.note_model(openai_request.get("model"))

        # Check if client disconnected before processing
        if await http_request.is_disconnected():
            raise HTTPException(status_code=499, detail="Client disconnected")

        if request.stream:
            # Optimistic start (mirrors the responses path): headers go out
            # immediately; upstream errors surface as SSE error events in the
            # converter. (create_chat_completion_stream is a lazy async
            # generator, so there is nothing to prime here.)
            openai_stream = openai_client.create_chat_completion_stream(
                openai_request, request_id
            )
            stats.record(
                "/v1/messages",
                status=200,
                latency=time.monotonic() - t0,  # accepted; tokens not counted for streams
            )
            return StreamingResponse(
                convert_openai_streaming_to_claude_with_cancellation(
                    openai_stream,
                    request,
                    logger,
                    http_request,
                    openai_client,
                    request_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Non-streaming response
            openai_response = await openai_client.create_chat_completion(
                openai_request, request_id
            )
            claude_response = convert_openai_to_claude_response(
                openai_response, request
            )
            usage = openai_response.get("usage") if isinstance(openai_response, dict) else None
            stats.record(
                "/v1/messages",
                status=200,
                latency=time.monotonic() - t0,
                usage=usage,
            )
            return claude_response
    except HTTPException as e:
        stats.record("/v1/messages", status=e.status_code, latency=time.monotonic() - t0)
        raise
    except Exception as e:
        import traceback

        logger.error(f"Unexpected error processing request: {e}")
        logger.error(traceback.format_exc())
        stats.record("/v1/messages", status=500, latency=time.monotonic() - t0)
        error_message = openai_client.classify_openai_error(str(e))
        raise HTTPException(status_code=500, detail=error_message)


async def _handle_responses_message(request: ClaudeMessagesRequest, http_request: Request, request_id: str, t0: float):
    """Serve /v1/messages via the Responses API upstream."""

    # Convert Claude request to Responses format
    responses_request = convert_claude_to_responses(request, model_manager)
    stats.note_model(responses_request.get("model"))

    # Free-tier gate rejects stream:false upstream (FreeTierError) even when
    # everything else matches, so always stream upstream. Non-streaming
    # clients get a de-streamed single response below.
    responses_request["stream"] = True

    # Check if client disconnected before processing
    if await http_request.is_disconnected():
        raise HTTPException(status_code=499, detail="Client disconnected")

    if request.stream:
        # Phase 2 optimistic start: return StreamingResponse immediately so
        # headers reach the client in milliseconds. The upstream POST runs
        # lazily inside the converter's pump task; an upstream rejection
        # after headers maps to an SSE error event there (same classified
        # message the pre-header JSON path used to return).
        responses_stream = responses_client.create_response_stream(
            responses_request, request_id
        )
        logger.debug("Stream %s headers out, upstream priming concurrently", request_id)
        stats.record("/v1/messages", status=200, latency=time.monotonic() - t0)
        return StreamingResponse(
            convert_responses_streaming_to_claude_with_cancellation(
                responses_stream,
                request,
                logger,
                http_request,
                responses_client,
                request_id,
            ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
    else:
        # Non-streaming client: stream upstream (gate requirement), then
        # accumulate the SSE events into one Responses object and convert.
        responses_object = await _collect_streamed_response(
            responses_client, responses_request, request_id, http_request
        )
        usage = responses_object.get("usage") if isinstance(responses_object, dict) else None
        stats.record("/v1/messages", status=200, latency=time.monotonic() - t0, usage=usage)
        return convert_responses_to_claude_response(responses_object, request)


async def _collect_streamed_response(
    responses_client, payload: dict, request_id: str, http_request: Request
) -> dict:
    """Stream a Responses request upstream and accumulate one response object.

    Used for non-streaming clients: the free-tier gate rejects ``stream:false``
    upstream, so we always stream and assemble the result here. Prefers the
    authoritative ``response.completed`` payload; falls back to assembling
    from deltas when the stream ends without one.
    """
    text_parts: list = []
    thinking_parts: list = []
    func_calls: dict = {}
    func_order: list = []
    usage: dict = {}
    status = "completed"
    resp_id: str | None = None
    gen = responses_client.create_response_stream(payload, request_id)
    try:
        async for raw_event in gen:
            if await http_request.is_disconnected():
                responses_client.cancel_request(request_id)
                raise HTTPException(status_code=499, detail="Client disconnected")
            event_type, data = _split_event(raw_event)
            if data is None:
                continue
            if event_type == "response.output_text.delta":
                if data.get("delta"):
                    text_parts.append(data["delta"])
            elif event_type == "response.reasoning_summary_text.delta":
                if data.get("delta"):
                    thinking_parts.append(data["delta"])
            elif event_type == "response.output_item.added":
                item = data.get("item", {}) or {}
                if item.get("type") == "function_call":
                    iid = item.get("id") or item.get("call_id") or f"fc_{uuid.uuid4().hex[:12]}"
                    if iid not in func_calls:
                        func_calls[iid] = {
                            "id": item.get("call_id") or item.get("id") or iid,
                            "name": item.get("name", ""),
                            "args": item.get("arguments") or "",
                        }
                        func_order.append(iid)
                    elif item.get("arguments"):
                        func_calls[iid]["args"] = item["arguments"]
            elif event_type == "response.function_call_arguments.delta":
                iid = data.get("item_id", "")
                frag = data.get("delta", "")
                if iid and frag:
                    entry = func_calls.setdefault(
                        iid, {"id": iid, "name": "", "args": ""}
                    )
                    if iid not in func_order:
                        func_order.append(iid)
                    entry["args"] += frag
            elif event_type in (
                "response.function_call_arguments.done",
                "response.output_item.done",
            ):
                item = data.get("item") if event_type == "response.output_item.done" else None
                if isinstance(item, dict) and item.get("type") == "function_call":
                    iid = item.get("id") or item.get("call_id", "")
                    entry = func_calls.get(iid)
                    if entry is not None and item.get("arguments"):
                        entry["args"] = item["arguments"]
                elif event_type == "response.function_call_arguments.done":
                    iid = data.get("item_id", "")
                    entry = func_calls.get(iid)
                    if entry is not None and data.get("arguments"):
                        entry["args"] = data["arguments"]
            elif event_type == "response.completed":
                response = data.get("response", {}) or {}
                # Authoritative full object: use it directly.
                return response
            elif event_type == "response.failed":
                raise HTTPException(
                    status_code=500, detail="Upstream response failed"
                )
    finally:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
    output: list = []
    if thinking_parts:
        output.append(
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "".join(thinking_parts)}],
            }
        )
    if text_parts:
        output.append(
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "".join(text_parts)}],
            }
        )
    for iid in func_order:
        entry = func_calls[iid]
        output.append(
            {
                "type": "function_call",
                "id": entry["id"],
                "call_id": entry["id"],
                "name": entry["name"],
                "arguments": entry["args"],
            }
        )
    return {
        "id": resp_id or f"resp_{uuid.uuid4().hex[:12]}",
        "output": output,
        "usage": usage,
        "status": status,
    }


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: ClaudeTokenCountRequest, http_request: Request, _: None = Depends(validate_api_key)):
    if body_too_large(http_request.headers, MAX_COUNT_TOKENS_BODY_BYTES):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        # Accurate estimator (text extraction + images + thinking budget +
        # tiktoken when installed, heuristic otherwise).
        from src.core.tokens import estimate_input_tokens

        estimated_tokens = estimate_input_tokens(
            model=getattr(request, "model", None),
            system=request.system,
            messages=request.messages,
            tools=request.tools,
            thinking=request.thinking,
        )

        return {"input_tokens": estimated_tokens}

    except Exception as e:
        logger.error(f"Error counting tokens: {e}")
        raise HTTPException(status_code=500, detail="Internal error")


@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "openai_api_configured": bool(config.openai_api_key),
        "api_key_valid": config.validate_api_key(),
        "client_api_key_validation": bool(config.anthropic_api_key),
    }


@router.get("/test-connection")
async def test_connection(_: None = Depends(validate_api_key)):
    """Test API connectivity to OpenAI"""
    try:
        # Simple test request to verify API connectivity
        test_response = await openai_client.create_chat_completion(
            {
                "model": config.small_model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 5,
            }
        )

        return {
            "status": "success",
            "message": "Successfully connected to OpenAI API",
            "model_used": config.small_model,
            "timestamp": datetime.now().isoformat(),
            "response_id": test_response.get("id", "unknown"),
        }

    except Exception as e:
        logger.error(f"API connectivity test failed: {e}")
        return JSONResponse(
            status_code=503,
            content={
                "status": "failed",
                "error_type": "API Error",
                "message": str(e),
                "timestamp": datetime.now().isoformat(),
                "suggestions": [
                    "Check your OPENAI_API_KEY is valid",
                    "Verify your API key has the necessary permissions",
                    "Check if you have reached rate limits",
                ],
            },
        )


@router.post("/v1/responses")
@router.post("/v1/responses/{path:path}")
async def passthrough_responses(
    request: Request, path: str = "", _: None = Depends(validate_api_key)
):
    """Passthrough for OpenAI Responses API (/v1/responses/*)"""
    if body_too_large(request.headers, MAX_PASSTHROUGH_BODY_BYTES):
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        safe_path = sanitize_passthrough_path(path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")
    body = await request.body()
    url = f"{config.openai_base_url}/responses"
    if safe_path:
        url += f"/{safe_path}"

    headers = {
        "Authorization": f"Bearer {config.openai_api_key}",
        "Content-Type": "application/json",
        **config.get_upstream_headers(),
    }
    # Forward query params
    params = dict(request.query_params)

    is_stream = False
    try:
        import json
        payload = json.loads(body)
        is_stream = payload.get("stream", False)
    except Exception:
        pass

    if is_stream:
        stats.record("/v1/responses", status=200)
        return StreamingResponse(
            _passthrough_stream_gen(request, url, body, headers, params),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            client = await get_shared_client()
            resp = await client.post(
                url, content=body, headers=headers, params=params,
                timeout=config.request_timeout,
            )
            stats.record("/v1/responses", status=resp.status_code)
            try:
                data = resp.json()
            except Exception:
                data = {
                    "error": {
                        "type": "api_error",
                        "message": scrub_api_keys(resp.text[:2000]),
                    }
                }
            return JSONResponse(status_code=resp.status_code, content=data)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Passthrough /v1/responses failed: {e}")
            stats.record("/v1/responses", status=502)
            return JSONResponse(
                status_code=502,
                content={"error": {"type": "api_error", "message": "Upstream unavailable, retry"}},
            )


async def _passthrough_stream_gen(request, url, body, headers, params):
    """Forward an upstream SSE stream with keepalive + disconnect hygiene.

    Errors (including upstream rejections) become a best-effort
    ``data: {"error": ...}`` frame — headers are already committed — plus
    stats; raw upstream bodies are never forwarded verbatim.
    """
    import json as _json

    keepalive = config.stream_keepalive_secs if config.stream_keepalive_secs > 0 else 15.0
    pending = None
    try:
        client = await get_shared_client()
        async with client.stream(
            "POST", url, content=body, headers=headers, params=params,
            timeout=get_stream_timeout(),
        ) as resp:
            if resp.status_code >= 400:
                err_body = await resp.aread()
                stats.record_stream_error(resp.status_code)
                yield _passthrough_error_frame(err_body.decode("utf-8", "replace"))
                return
            it = resp.aiter_bytes()
            while True:
                if pending is None:
                    pending = asyncio.create_task(it.__anext__())
                try:
                    # Shield the read: a keepalive timeout must not cancel
                    # the in-flight chunk (the same task is re-awaited).
                    chunk = await asyncio.wait_for(asyncio.shield(pending), timeout=keepalive)
                except StopAsyncIteration:
                    pending = None
                    break
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    stats.record_keepalive()
                    yield b": ping\n\n"
                    continue
                pending = None
                if chunk:
                    yield chunk
                if await request.is_disconnected():
                    break
    except HTTPException as e:
        stats.record_stream_error(e.status_code or 500)
        yield _passthrough_error_frame(str(e.detail))
    except Exception as e:
        logger.error(f"Passthrough stream failed: {e}")
        stats.record_stream_error(502)
        yield _passthrough_error_frame("Upstream unavailable, retry")
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


def _passthrough_error_frame(detail: str) -> bytes:
    """Best-effort OpenAI-protocol error frame (classified, scrubbed)."""
    import json as _json

    from src.core.responses_client import classify_responses_error

    message = scrub_api_keys(classify_responses_error(detail)[-2000:])
    return ("data: " + _json.dumps({"error": {"message": message}}) + "\n\n").encode()


@router.post("/v1/chat/completions")
async def passthrough_chat_completions(
    request: Request, _: None = Depends(validate_api_key)
):
    """Passthrough for OpenAI Chat Completions API (/v1/chat/completions)"""
    if body_too_large(request.headers, MAX_PASSTHROUGH_BODY_BYTES):
        raise HTTPException(status_code=413, detail="Request body too large")
    body = await request.body()
    url = f"{config.openai_base_url}/chat/completions"

    headers = {
        "Authorization": f"Bearer {config.openai_api_key}",
        "Content-Type": "application/json",
        **config.get_upstream_headers(),
    }
    params = dict(request.query_params)

    is_stream = False
    try:
        import json
        payload = json.loads(body)
        is_stream = payload.get("stream", False)
    except Exception:
        pass

    if is_stream:
        stats.record("/v1/chat/completions", status=200)
        return StreamingResponse(
            _passthrough_stream_gen(request, url, body, headers, params),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        try:
            client = await get_shared_client()
            resp = await client.post(
                url, content=body, headers=headers, params=params,
                timeout=config.request_timeout,
            )
            stats.record("/v1/chat/completions", status=resp.status_code)
            try:
                data = resp.json()
            except Exception:
                data = {
                    "error": {
                        "type": "api_error",
                        "message": scrub_api_keys(resp.text[:2000]),
                    }
                }
            return JSONResponse(status_code=resp.status_code, content=data)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Passthrough /v1/chat/completions failed: {e}")
            stats.record("/v1/chat/completions", status=502)
            return JSONResponse(
                status_code=502,
                content={"error": {"type": "api_error", "message": "Upstream unavailable, retry"}},
            )


@router.get("/api/status")
async def api_status():
    """Machine-readable status for the dashboard (also used by scripts).

    `last_upstream_failure` may be in the legacy shape ({status, payload})
    from before the compact error-first dump; the dashboard only reads the
    new shape ({status, error, model, request_id, at}).
    """
    last_failure = None
    failure_history: list = []
    import glob as _glob
    import json as _json

    try:
        log_dir = os.environ.get("PROXY_LOG_DIR", "logs")
        path = os.path.join(log_dir, "last_upstream_failure.json")
        if os.path.exists(path):
            with open(path) as f:
                last_failure = _json.load(f)
            if isinstance(last_failure, dict):
                # Scrub key-like fragments upstream echoes in errors.
                if last_failure.get("error"):
                    last_failure["error"] = scrub_api_keys(last_failure["error"])
            if isinstance(last_failure, dict) and "at" not in last_failure:
                # Legacy dump: no timestamp, no error text. Treat as stale
                # but keep the status so old watchers still see a 429.
                last_failure = {
                    "status": last_failure.get("status"),
                    "error": "(legacy dump: error text not recorded)",
                    "model": None,
                    "request_id": None,
                    "at": 0,
                }
    except Exception:
        last_failure = None
    try:
        arch = sorted(
            _glob.glob(os.path.join(log_dir, "failures", "failure-*.json")),
            reverse=True,
        )[:20]
        for ap in arch:
            try:
                with open(ap) as f:
                    d = _json.load(f)
                if isinstance(d, dict):
                    failure_history.append(
                        {
                            "status": d.get("status"),
                            "error": scrub_api_keys(str(d.get("error", ""))[:300]),
                            "model": d.get("model"),
                            "at": d.get("at", 0),
                        }
                    )
            except Exception:
                continue
    except Exception:
        failure_history = []
    return {
        "proxy": {
            "openai_base_url": config.openai_base_url,
            "wire_api": config.upstream_wire_api,
            "upstream_user_agent": config.upstream_user_agent,
            "max_tokens_limit": config.max_tokens_limit,
            "request_timeout": config.request_timeout,
            "retry_budget_secs": config.responses_retry_budget_secs,
            "keepalive_secs": config.stream_keepalive_secs,
            "client_key_validation": bool(config.anthropic_api_key),
            "models": {
                "big": config.big_model,
                "middle": config.middle_model,
                "small": config.small_model,
            },
        },
        "stats": stats.snapshot(),
        "last_upstream_failure": last_failure,
        "failure_history": failure_history,
    }


@router.get("/")
async def root():
    """Native status dashboard (HTML). JSON moved to /api/status."""
    return dashboard_response()
