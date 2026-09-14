"""Upstream-safe tool name aliasing.

Claude Code (and MCP) tool names routinely exceed the 64-char `name` limit
enforced by OpenAI-compatible upstreams (chat completions `function.name`
and Responses `function.name` both reject longer values with
`invalid_request_error`: "`name` must be at most 64 characters").

This module maps over-long (or charset-invalid) tool names to short,
deterministic upstream aliases on the request path and maps them back to
the original Claude-side names on the response path, so tool_use blocks
keep working end to end.

Alias scheme: `<sanitized-stem>_<8-hex-sha1>` truncated to 64 chars total.
Valid names pass through untouched (no registry entry needed).
"""

import hashlib
import logging
import re
import threading

logger = logging.getLogger(__name__)

MAX_TOOL_NAME_LEN = 64
_HASH_LEN = 8
_VALID_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

_registry: dict = {}
_lock = threading.Lock()


def to_upstream_name(name: str) -> str:
    """Return an upstream-safe alias for a Claude-side tool name.

    Names already valid for the upstream wire format are returned as-is.
    Anything else gets a deterministic alias, registered for the reverse
    lookup on the response path.
    """
    if not name:
        return name
    if _VALID_NAME.match(name):
        return name
    stem = _INVALID_CHARS.sub("_", name).strip("_") or "tool"
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:_HASH_LEN]
    room = MAX_TOOL_NAME_LEN - _HASH_LEN - 1
    alias = f"{stem[:room]}_{digest}"
    logger.debug("Aliasing over-long tool name %r -> %r", name, alias)
    with _lock:
        existing = _registry.get(alias)
        if existing is not None and existing != name:
            # Practically impossible (sha1 collision on distinct stems that
            # also truncate identically), but never silently misroute a call.
            logger.error(
                "Tool name alias collision: %r and %r both map to %r; "
                "keeping first mapping",
                existing,
                name,
                alias,
            )
            return alias
        _registry[alias] = name
    return alias


def from_upstream_name(name: str) -> str:
    """Map an upstream alias back to the original Claude-side tool name."""
    if not name:
        return name
    with _lock:
        return _registry.get(name, name)


def registered_aliases() -> dict:
    """Snapshot of the alias -> original registry (introspection/tests)."""
    with _lock:
        return dict(_registry)
