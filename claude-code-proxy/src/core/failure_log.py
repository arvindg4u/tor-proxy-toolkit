"""Persistent upstream-failure log for the status dashboard.

The dashboard needs an honest, recent view of why upstream calls fail. The
old implementation only wrote a single ``logs/last_upstream_failure.json``
from the Responses path, so chat-wire failures were invisible and the
"history" panel was always empty.

This module keeps the same single-record file for compatibility and archives
the previous record into ``logs/failures/`` on every new failure. It is
intentionally dependency-free and never raises: failure logging must not
break the request path.
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
import time
from typing import Any, Dict, Optional

_MAX_HISTORY = 50


def _log_dir() -> str:
    return os.environ.get("PROXY_LOG_DIR", "logs")


def _safe_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".failure-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def _archive_current(last_path: str) -> None:
    """Archive the existing last-failure record, if any."""
    old = _safe_json(last_path)
    if not old:
        return
    status = old.get("status")
    at = old.get("at")
    if status is None or not isinstance(at, (int, float)) or at <= 0:
        return
    failures_dir = os.path.join(os.path.dirname(last_path), "failures")
    os.makedirs(failures_dir, exist_ok=True)
    try:
        os.chmod(failures_dir, 0o700)
    except OSError:
        pass
    dest = os.path.join(
        failures_dir,
        f"failure-{int(float(at) * 1000)}-{str(status).replace('/', '_')}.json",
    )
    if os.path.exists(dest):
        return
    _atomic_write_json(dest, old)


def _prune_history(failures_dir: str) -> None:
    try:
        files = sorted(
            glob.glob(os.path.join(failures_dir, "failure-*.json")),
            key=os.path.getmtime,
            reverse=True,
        )
        for path in files[_MAX_HISTORY:]:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:
        pass


def record_upstream_failure(
    status: int,
    error: str,
    *,
    model: Optional[str] = None,
    request_id: Optional[str] = None,
    ttl: Optional[str] = None,
    shape: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one final upstream failure and archive the previous one.

    ``error`` is scrubbed of key-like fragments before it is written. The
    current record keeps the historical shape (status/error/model/request_id/
    cache_ttl/shape/at) so existing scripts and the legacy dashboard continue
    to work.
    """
    try:
        try:
            from src.core.guards import scrub_api_keys
        except Exception:
            def scrub_api_keys(value: str) -> str:  # type: ignore
                return str(value or "")

        log_dir = _log_dir()
        os.makedirs(log_dir, exist_ok=True)
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass
        last_path = os.path.join(log_dir, "last_upstream_failure.json")
        _archive_current(last_path)
        _prune_history(os.path.join(log_dir, "failures"))

        payload = {
            "status": int(status),
            "error": scrub_api_keys(str(error or ""))[:2000],
            "model": model,
            "request_id": request_id,
            "cache_ttl": ttl,
            "shape": shape or {},
            "at": time.time(),
        }
        _atomic_write_json(last_path, payload)
    except Exception:
        # Logging is best-effort and must never mask the real request error.
        pass
