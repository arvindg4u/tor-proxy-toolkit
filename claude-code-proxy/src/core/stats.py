"""In-memory request statistics for the status dashboard.

Single-process counters only (no persistence). Tokens are tracked for
non-streaming responses where upstream returns a usage block; streaming
responses contribute request counts but no token counts.
"""

import math
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional


# Max conversations tracked for the message_start usage base. Bounds memory
# when many sessions share one proxy process; the oldest conversation's
# base is evicted first (its next turn simply starts from zero again).
_MAX_USAGE_KEYS = 16


def _block_text(block: Any, limit: int = 500) -> str:
    """Best-effort text of one content block (pydantic or plain dict)."""
    if isinstance(block, str):
        return block[:limit]
    if hasattr(block, "type"):
        if getattr(block, "type") != "text":
            return ""
        return str(getattr(block, "text", "") or "")[:limit]
    if isinstance(block, dict):
        if block.get("type") != "text":
            return ""
        return str(block.get("text", "") or "")[:limit]
    return ""


def _message_text(msg: Any, limit: int = 500) -> str:
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        return "".join(_block_text(b) for b in content)[:limit]
    return ""


def request_usage_key(request: Any) -> str:
    """Fingerprint one conversation for the message_start usage base.

    The Messages API is stateless — no session id travels on the wire — so
    concurrent CLI sessions are told apart by (model, system prompt head,
    first-turn text). Only the conversation *prefix* is used because turns
    only append messages: anything that changes turn to turn (turn count,
    latest text) would make each turn look like a new conversation and the
    base would never carry forward. Two sessions opened with byte-identical
    first prompts share a key (rare; message_delta corrects any remainder
    at turn end regardless).
    """
    import hashlib

    model = str(getattr(request, "model", "") or "")
    system = getattr(request, "system", None)
    if isinstance(system, str):
        system_text = system
    elif isinstance(system, list):
        system_text = "".join(_block_text(b, limit=2000) for b in system)
    elif isinstance(system, dict):
        system_text = str(system.get("text", ""))
    else:
        system_text = ""
    messages = getattr(request, "messages", None) or []
    if isinstance(messages, dict):
        messages = messages.get("messages", []) or []
    first = _message_text(messages[0]) if isinstance(messages, list) and messages else ""
    h = hashlib.sha1()
    for part in (model, system_text[:2000], first):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _percentile(sorted_vals: List[float], q: float) -> float:
    """Nearest-rank percentile of a pre-sorted list (q in 0..1)."""
    if not sorted_vals:
        return 0.0
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return sorted_vals[idx]


def _summarize(samples: deque, total_sum: float, total_count: int) -> Dict[str, Any]:
    """Summarize latency samples (seconds) as milliseconds.

    avg/min/max/p50/p95 describe the bounded recent window (deque maxlen);
    count covers all time. avg is over the window, not all time.
    """
    if samples:
        ordered = sorted(samples)
        window_avg_ms = round(sum(ordered) / len(ordered) * 1000, 1)
        return {
            "count": total_count,
            "avg_ms": window_avg_ms,
            "min_ms": round(ordered[0] * 1000, 1),
            "p50_ms": round(_percentile(ordered, 0.5) * 1000, 1),
            "p95_ms": round(_percentile(ordered, 0.95) * 1000, 1),
            "max_ms": round(ordered[-1] * 1000, 1),
        }
    avg_ms = round(total_sum / total_count * 1000, 1) if total_count else 0.0
    return {
        "count": total_count,
        "avg_ms": avg_ms,
        "min_ms": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "max_ms": 0.0,
    }


class ProxyStats:
    """Thread-safe counters."""

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.total_requests = 0
        self.ok_requests = 0
        self.errors = 0
        self.by_endpoint: Dict[str, int] = {}
        self.by_model: Dict[str, int] = {}
        self.by_status: Dict[str, int] = {}
        self.tokens_in = 0
        self.tokens_out = 0
        self.tokens_cached = 0
        # Last completed response's usage per conversation key (Claude-shape
        # keys); reported in the next message_start so the live counter
        # stays continuous. LRU-capped: concurrent sessions each keep
        # their own base instead of borrowing each other's.
        self._last_usage: Dict[str, Dict[str, int]] = {}
        self.latency_sum = 0.0
        self.latency_count = 0
        self.last_error_at: Optional[float] = None
        # Streaming liveness (Phase 0): TTFT = accept -> first token,
        # ITL = inter-token gap. Recent samples bounded for p50/p95.
        self.streams = 0
        self.ttft_sum = 0.0
        self.ttft_count = 0
        self.ttft_samples: deque = deque(maxlen=200)
        self.itl_sum = 0.0
        self.itl_count = 0
        self.itl_samples: deque = deque(maxlen=500)
        self.keepalive_sent = 0

    def record(
        self,
        endpoint: str,
        model: Optional[str] = None,
        status: int = 200,
        latency: Optional[float] = None,
        usage: Optional[Dict[str, Any]] = None,
    ):
        """Record one completed request."""
        with self._lock:
            self.total_requests += 1
            if 200 <= status < 400:
                self.ok_requests += 1
            else:
                self.errors += 1
                self.last_error_at = time.time()
            self.by_endpoint[endpoint] = self.by_endpoint.get(endpoint, 0) + 1
            self.by_status[str(status)] = self.by_status.get(str(status), 0) + 1
            if model:
                if model not in self.by_model and len(self.by_model) >= 200:
                    pass
                else:
                    self.by_model[model] = self.by_model.get(model, 0) + 1
            if latency is not None:
                self.latency_sum += latency
                self.latency_count += 1
            if usage:
                # OpenAI chat shape (prompt/completion) and Responses shape (input/output)
                self.tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                self.tokens_out += int(
                    usage.get("completion_tokens") or usage.get("output_tokens") or 0
                )
                self.tokens_cached += int(
                    usage.get("cache_read_input_tokens", 0)
                    or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                    or (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
                )

    def note_model(self, model: Optional[str]):
        """Count a request against a model name (endpoint counting happens elsewhere)."""
        if not model:
            return
        with self._lock:
            # Cap cardinality: untrusted model names must not grow this dict
            # without bound.
            if model not in self.by_model and len(self.by_model) >= 200:
                return
            self.by_model[model] = self.by_model.get(model, 0) + 1

    def record_stream_error(self, status: int = 500) -> None:
        """Count a post-header streaming failure.

        The accept was already recorded as 200 (optimistic start), so this
        only bumps errors/by_status/last_error_at — never total_requests.
        """
        try:
            code = int(status)
        except (TypeError, ValueError):
            code = 500
        with self._lock:
            self.errors += 1
            self.last_error_at = time.time()
            key = str(code)
            self.by_status[key] = self.by_status.get(key, 0) + 1

    def record_stream(self) -> None:
        """Count one started streaming response."""
        with self._lock:
            self.streams += 1

    def record_ttft(self, latency_secs: float) -> None:
        """Record time-to-first-token for one stream (seconds)."""
        if not isinstance(latency_secs, (int, float)):
            return
        if not math.isfinite(latency_secs) or latency_secs < 0:
            return
        with self._lock:
            self.ttft_sum += latency_secs
            self.ttft_count += 1
            self.ttft_samples.append(latency_secs)

    def record_itl(self, latency_secs: float) -> None:
        """Record one inter-token gap (seconds)."""
        if not isinstance(latency_secs, (int, float)):
            return
        if not math.isfinite(latency_secs) or latency_secs < 0:
            return
        with self._lock:
            self.itl_sum += latency_secs
            self.itl_count += 1
            self.itl_samples.append(latency_secs)

    def record_keepalive(self, count: int = 1) -> None:
        """Count SSE keepalive pings sent on otherwise-silent streams."""
        if not isinstance(count, int) or count <= 0:
            return
        with self._lock:
            self.keepalive_sent += count

    def note_response_usage(
        self, usage: Optional[Dict[str, Any]], key: str = "default"
    ) -> None:
        """Remember a completed response's usage for message_start.

        The Claude client builds its live context counter from the usage in
        ``message_start`` and only corrects it at ``message_delta``. Real
        Anthropic sends the true (cumulative) input counts there; sending
        zeros makes the displayed counter collapse at every turn start and
        jump back at turn end. The upstream only reports usage at the end
        of a response, so the previous turn's totals are the best base for
        the next turn's live display.

        ``key`` scopes the snapshot per conversation (see
        :func:`request_usage_key`) so concurrent sessions never borrow
        each other's base. Entries are LRU-capped; a full cache evicts the
        oldest conversation first.
        """
        if not usage:
            return
        with self._lock:
            if key in self._last_usage:
                del self._last_usage[key]
            elif len(self._last_usage) >= _MAX_USAGE_KEYS:
                oldest = next(iter(self._last_usage))
                del self._last_usage[oldest]
            self._last_usage[key] = {
                "input_tokens": int(
                    usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                ),
                "output_tokens": 0,  # nothing generated yet this turn
                "cache_read_input_tokens": int(
                    usage.get("cache_read_input_tokens", 0)
                    or (usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens", 0
                    )
                    or (usage.get("input_tokens_details") or {}).get(
                        "cached_tokens", 0
                    )
                ),
                "cache_creation_input_tokens": int(
                    usage.get("cache_creation_input_tokens", 0)
                ),
            }

    def last_response_usage(self, key: str = "default") -> Dict[str, int]:
        """Usage base for the next message_start (zeros before first use)."""
        with self._lock:
            return dict(
                self._last_usage.get(
                    key,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                )
            )

    def add_tokens(
        self, usage: Optional[Dict[str, Any]], key: str = "default"
    ):
        """Add token usage without counting a new request."""
        self.note_response_usage(usage, key=key)
        if not usage:
            return
        with self._lock:
            self.tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            self.tokens_out += int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )
            self.tokens_cached += int(
                usage.get("cache_read_input_tokens", 0)
                or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                or (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
            )

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable copy of all counters."""
        with self._lock:
            avg_latency = self.latency_sum / self.latency_count if self.latency_count else 0.0
            ttft = _summarize(self.ttft_samples, self.ttft_sum, self.ttft_count)
            itl = _summarize(self.itl_samples, self.itl_sum, self.itl_count)
            return {
                "uptime_secs": time.time() - self.started_at,
                "started_at": self.started_at,
                "total_requests": self.total_requests,
                "ok_requests": self.ok_requests,
                "errors": self.errors,
                "error_rate": (self.errors / self.total_requests) if self.total_requests else 0.0,
                "last_error_at": self.last_error_at,
                "avg_latency_ms": round(avg_latency * 1000, 1),
                "streams": self.streams,
                "ttft_ms": ttft,
                "itl_ms": itl,
                "keepalive_sent": self.keepalive_sent,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "tokens_cached": self.tokens_cached,
                "by_endpoint": dict(self.by_endpoint),
                "by_model": dict(self.by_model),
                "by_status": dict(self.by_status),
            }


stats = ProxyStats()
