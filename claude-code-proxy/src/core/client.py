import asyncio
import logging
import os
from fastapi import HTTPException
from typing import Optional, AsyncGenerator, Dict, Any
from openai import AsyncOpenAI, AsyncAzureOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai._exceptions import APIError, RateLimitError, AuthenticationError, BadRequestError

logger = logging.getLogger(__name__)

# IP-abuse recovery: opencode gates a flagged egress IP with a 403
# FreeTierError ("detected abusive traffic from this IP address"). The flag
# is IP-scoped (verified live: same key/session/headers pass on a different
# peer), so the cure is a fresh peer — and auto.sh already rotates on this
# exact error (its scan runs every WG_POLL=10s and owns rotation). Rather
# than a proxy-side rotation colliding with auto.sh's, we wait for auto.sh's
# rotation to complete (new "live:" line in auto.log) and retry once.
_WIREPROXY_DIR = os.environ.get("WIREPROXY_DIR", "/home/ubuntu/wgtunnel/wireproxy")
_AUTO_LOG = os.path.join(_WIREPROXY_DIR, "auto.log")


def _live_line() -> str:
    """Last 'live: ...' line in auto.log (peer rotation marker), or ''."""
    try:
        with open(_AUTO_LOG, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            blob = fh.read().decode("utf-8", "ignore")
        hits = [ln for ln in blob.splitlines() if ln.startswith("live:")]
        return hits[-1] if hits else ""
    except Exception:
        return ""


async def _await_peer_rotation(max_wait: float = 60.0) -> bool:
    """Wait for auto.sh to rotate the wireproxy peer. True = a new egress IP
    is confirmed live (a 'live:' line different from the one we anchored on
    appears). Caller requests are long-lived, so the wait is cheap relative
    to a hard failure shown to the user."""
    before = _live_line()
    if not before:
        return False
    for _ in range(int(max_wait)):
        await asyncio.sleep(1)
        now = _live_line()
        if now and now != before:
            return True
    return False


async def _recover_ip_abuse_403(e: APIError) -> bool:
    """True only when this is the IP-abuse FreeTierError AND a rotation is
    observed — the caller may then retry the upstream attempt exactly once."""
    try:
        if getattr(e, "status_code", None) != 403:
            return False
        if "abusive traffic from this ip" not in str(e).lower():
            return False
        logger.warning("opencode IP-abuse 403 seen; waiting for auto.sh rotation before one retry")
        ok = await _await_peer_rotation()
        if ok:
            logger.info("peer rotation confirmed; retrying once")
        else:
            logger.warning("rotation not observed in 60s; surfacing original error")
        return ok
    except Exception:
        return False

class OpenAIClient:
    """Async OpenAI client with cancellation support."""
    
    def __init__(self, api_key: str, base_url: str, timeout: int = 90, api_version: Optional[str] = None, custom_headers: Optional[Dict[str, str]] = None):
        self.api_key = api_key
        self.base_url = base_url
        self.custom_headers = custom_headers or {}
        
        # Prepare default headers
        default_headers = {
            "Content-Type": "application/json",
            "User-Agent": "claude-proxy/1.0.0"
        }
        
        # Merge custom headers with default headers
        all_headers = {**default_headers, **self.custom_headers}
        
        # Detect if using Azure and instantiate the appropriate client
        if api_version:
            self.client = AsyncAzureOpenAI(
                api_key=api_key,
                azure_endpoint=base_url,
                api_version=api_version,
                timeout=timeout,
                default_headers=all_headers
            )
        else:
            self.client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                default_headers=all_headers
            )
        self.active_requests: Dict[str, asyncio.Event] = {}
    
    async def create_chat_completion(self, request: Dict[str, Any], request_id: Optional[str] = None, _rotate_retried: bool = False) -> Dict[str, Any]:
        """Send chat completion to OpenAI API with cancellation support."""
        
        # Create cancellation token if request_id provided
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event
        
        try:
            # Create task that can be cancelled
            completion_task = asyncio.create_task(
                self.client.chat.completions.create(**request)
            )
            
            if request_id:
                # Wait for either completion or cancellation
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, pending = await asyncio.wait(
                    [completion_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                # Check if request was cancelled
                if cancel_task in done:
                    completion_task.cancel()
                    raise HTTPException(status_code=499, detail="Request cancelled by client")
                
                completion = await completion_task
            else:
                completion = await completion_task
            
            # Convert to dict format that matches the original interface
            return completion.model_dump()
        
        except AuthenticationError as e:
            raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))
        except RateLimitError as e:
            raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))
        except BadRequestError as e:
            raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            if not _rotate_retried and await _recover_ip_abuse_403(e):
                # Fresh peer confirmed: one transparent retry through the
                # same call path (recursion depth capped by the flag).
                return await self.create_chat_completion(request, request_id, _rotate_retried=True)
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
        
        finally:
            # Clean up active request tracking
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]
    
    async def create_chat_completion_stream(self, request: Dict[str, Any], request_id: Optional[str] = None, _rotate_retried: bool = False) -> AsyncGenerator[Dict[str, Any], None]:
        """Send streaming chat completion to OpenAI API with cancellation support.

        Phase 3: yields decoded chunk dicts (``{"type": "chunk", "chunk": {...}}``,
        ``{"type": "done"}``) instead of pre-serialized ``"data: ..."`` strings,
        so the converter serializes exactly once at the SSE boundary.
        """
        
        # Create cancellation token if request_id provided
        if request_id:
            cancel_event = asyncio.Event()
            self.active_requests[request_id] = cancel_event
        
        try:
            # Ensure stream is enabled
            request["stream"] = True
            if "stream_options" not in request:
                request["stream_options"] = {}
            request["stream_options"]["include_usage"] = True
            
            # Create the streaming completion. The opencode gate answers a
            # flagged egress IP with 403 (IP-abuse FreeTierError) at connect
            # time — nothing is yielded yet, so wait for auto.sh's rotation
            # and retry once here, fully transparent to the client.
            try:
                streaming_completion = await self.client.chat.completions.create(**request)
            except APIError as e:
                if not _rotate_retried and await _recover_ip_abuse_403(e):
                    streaming_completion = await self.client.chat.completions.create(**request)
                else:
                    raise
            
            async for chunk in streaming_completion:
                # Check for cancellation before yielding each chunk
                if request_id and request_id in self.active_requests:
                    if self.active_requests[request_id].is_set():
                        raise HTTPException(status_code=499, detail="Request cancelled by client")
                
                # Convert chunk to a decoded dict (no JSON round-trip here;
                # the converter serializes once at the SSE boundary).
                chunk_dict = chunk.model_dump()
                yield {"type": "chunk", "chunk": chunk_dict}

            # Signal end of stream
            yield {"type": "done"}
                
        except AuthenticationError as e:
            raise HTTPException(status_code=401, detail=self.classify_openai_error(str(e)))
        except RateLimitError as e:
            raise HTTPException(status_code=429, detail=self.classify_openai_error(str(e)))
        except BadRequestError as e:
            raise HTTPException(status_code=400, detail=self.classify_openai_error(str(e)))
        except APIError as e:
            status_code = getattr(e, 'status_code', 500)
            raise HTTPException(status_code=status_code, detail=self.classify_openai_error(str(e)))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
        
        finally:
            # Clean up active request tracking
            if request_id and request_id in self.active_requests:
                del self.active_requests[request_id]

    def classify_openai_error(self, error_detail: Any) -> str:
        """Provide specific error guidance for common OpenAI API issues."""
        error_str = str(error_detail).lower()
        
        # Region/country restrictions
        if "unsupported_country_region_territory" in error_str or "country, region, or territory not supported" in error_str:
            return "OpenAI API is not available in your region. Consider using a VPN or Azure OpenAI service."
        
        # API key issues
        if "invalid_api_key" in error_str or "unauthorized" in error_str:
            return "Invalid API key. Please check your OPENAI_API_KEY configuration."
        
        # Rate limiting
        if "rate_limit" in error_str or "quota" in error_str:
            return "Rate limit exceeded. Please wait and try again, or upgrade your API plan."
        
        # Model not found
        if "model" in error_str and ("not found" in error_str or "does not exist" in error_str):
            return "Model not found. Please check your BIG_MODEL and SMALL_MODEL configuration."
        
        # Billing issues
        if "billing" in error_str or "payment" in error_str:
            return "Billing issue. Please check your OpenAI account billing status."
        
        # Default: return original message
        return str(error_detail)
    
    def cancel_request(self, request_id: str) -> bool:
        """Cancel an active request by request_id."""
        if request_id in self.active_requests:
            self.active_requests[request_id].set()
            return True
        return False