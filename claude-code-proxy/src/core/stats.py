"""In-memory request statistics for the status dashboard.

Single-process counters only (no persistence). Tokens are tracked for
non-streaming responses where upstream returns a usage block; streaming
responses contribute request counts but no token counts.
"""

import threading
import time
from typing import Any, Dict, Optional


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
        self.latency_sum = 0.0
        self.latency_count = 0
        self.last_error_at: Optional[float] = None

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

    def note_model(self, model: Optional[str]):
        """Count a request against a model name (endpoint counting happens elsewhere)."""
        if not model:
            return
        with self._lock:
            self.by_model[model] = self.by_model.get(model, 0) + 1

    def add_tokens(self, usage: Optional[Dict[str, Any]]):
        """Add token usage without counting a new request."""
        if not usage:
            return
        with self._lock:
            self.tokens_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            self.tokens_out += int(
                usage.get("completion_tokens") or usage.get("output_tokens") or 0
            )

    def snapshot(self) -> Dict[str, Any]:
        """Return a JSON-serializable copy of all counters."""
        with self._lock:
            avg_latency = self.latency_sum / self.latency_count if self.latency_count else 0.0
            return {
                "uptime_secs": time.time() - self.started_at,
                "started_at": self.started_at,
                "total_requests": self.total_requests,
                "ok_requests": self.ok_requests,
                "errors": self.errors,
                "error_rate": (self.errors / self.total_requests) if self.total_requests else 0.0,
                "last_error_at": self.last_error_at,
                "avg_latency_ms": round(avg_latency * 1000, 1),
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "by_endpoint": dict(self.by_endpoint),
                "by_model": dict(self.by_model),
                "by_status": dict(self.by_status),
            }


stats = ProxyStats()
