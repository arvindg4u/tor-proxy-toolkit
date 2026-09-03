"""Async client for the OpenAI Responses API (used on the responses wire path)."""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Optional

import httpx
from fastapi import HTTPException


def classify_responses_error(error_detail: Any) -> str:
    """Provide specific guidance for common Responses API failures."""
    error_str = str(error_detail).lower()

    if "freelimit" in error_str or "free usage" in error_str or "429" in error_str:
        return (
            "Free-tier rate limit exceeded. Wait before retrying, or check the "
            "upstream User-Agent allowlist (bot UAs get a tiny quota)."
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


class ResponsesClient:
    """Async Responses API client with cancellation support."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: int = 90,
        user_agent: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
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
        self.active_requests: Dict[str, asyncio.Event] = {}

    def _url(self) -> str:
        return f"{self.base_url}/responses"

    async def create_response(
        self, payload: Dict[str, Any], request_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """POST a Responses request and return the decoded JSON object."""
        if request_id:
            self.active_requests[request_id] = asyncio.Event()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url(), json=payload, headers=self.headers)
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=classify_responses_error(resp.text),
                )
            data = resp.json()
            if isinstance(data, dict) and data.get("type") == "error":
                raise HTTPException(
                    status_code=500,
                    detail=classify_responses_error(json.dumps(data)),
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
        """
        if request_id:
            self.active_requests[request_id] = asyncio.Event()
        try:
            body = dict(payload)
            body["stream"] = True
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream(
                    "POST", self._url(), json=body, headers=self.headers
                ) as resp:
                    if resp.status_code >= 400:
                        err_body = await resp.aread()
                        raise HTTPException(
                            status_code=resp.status_code,
                            detail=classify_responses_error(
                                err_body.decode("utf-8", "replace")
                            ),
                        )
                    event_type: Optional[str] = None
                    data_lines: list = []
                    async for line in resp.aiter_lines():
                        if request_id and self.active_requests.get(request_id) is not None:
                            if self.active_requests[request_id].is_set():
                                raise HTTPException(
                                    status_code=499,
                                    detail="Request cancelled by client",
                                )
                        if not line.strip():
                            if event_type is not None or data_lines:
                                data = "\n".join(data_lines)
                                yield f"event: {event_type or 'message'}\ndata: {data}"
                            event_type, data_lines = None, []
                            continue
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                        # ignore ":" comments and other fields
                    if event_type is not None or data_lines:
                        data = "\n".join(data_lines)
                        yield f"event: {event_type or 'message'}\ndata: {data}"
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
