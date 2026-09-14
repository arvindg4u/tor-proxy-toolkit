"""Small request guards shared by endpoints (stdlib only, no heavy imports)."""

import re
from typing import Mapping

_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9\-_]{3,}")


def scrub_api_keys(text: str) -> str:
    """Replace key-like `sk-...` fragments so logs/status never leak them."""
    if not text:
        return ""
    return _API_KEY_RE.sub("sk-***", str(text))


def sanitize_passthrough_path(path: str) -> str:
    """Validate an upstream sub-path; return the cleaned path.

    Rejects directory traversal (including percent-encoded variants),
    leftover percent signs (double-encoding), query/fragment smuggling,
    and control characters. Raises ValueError on rejection.
    """
    if not path:
        return ""
    from urllib.parse import unquote

    decoded = unquote(path)
    segments = decoded.split("/")
    if any(seg == ".." for seg in segments):
        raise ValueError("path traversal rejected")
    if "%" in decoded:
        raise ValueError("encoded path rejected")
    if any(c in decoded for c in ("?", "#", "\\")):
        raise ValueError("invalid path characters")
    if re.search(r"[\x00-\x1f\x7f]", decoded):
        raise ValueError("invalid path characters")
    return "/".join(seg for seg in segments if seg not in ("", "."))


def body_too_large(headers: Mapping, limit_bytes: int) -> bool:
    """True when Content-Length exceeds the limit (missing/unparsable = False)."""
    try:
        length = int(headers.get("content-length", 0) or 0)
    except (TypeError, ValueError):
        return False
    return length > limit_bytes
