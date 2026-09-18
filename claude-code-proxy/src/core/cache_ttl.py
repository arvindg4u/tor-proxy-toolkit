"""P5: prompt-cache TTL detection + beta allowlist.

Official: cache breakpoints carry cache_control {type: ephemeral, ttl: 5m|1h}.
1h needs `anthropic-beta: extended-cache-ttl-2025-04-11`. Default regressed
1h -> 5m, causing 15-53% overpay when 1h intent is silently served as 5m.
"""

from typing import Any, Optional

EXTENDED_CACHE_TTL_BETA = "extended-cache-ttl-2025-04-11"


def _ttl_of(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    cc = obj.get("cache_control") if isinstance(obj, dict) else getattr(obj, "cache_control", None)
    if cc is None:
        return None
    ttl = cc.get("ttl") if isinstance(cc, dict) else getattr(cc, "ttl", None)
    if ttl in ("1h", "5m"):
        return ttl
    # Bare {"type": "ephemeral"} with no ttl means 5m.
    if isinstance(cc, dict) and cc.get("type") == "ephemeral":
        return "5m"
    if getattr(cc, "type", None) == "ephemeral":
        return "5m"
    return None


def effective_ttl(request: Any, default: str = "5m") -> str:
    """1h if any breakpoint asks for it, else default (5m)."""
    candidates: list[str] = []
    try:
        system = getattr(request, "system", None)
        if isinstance(system, list):
            for b in system:
                t = _ttl_of(b)
                if t:
                    candidates.append(t)
        for m in getattr(request, "messages", None) or []:
            content = getattr(m, "content", None)
            if isinstance(content, list):
                for b in content:
                    t = _ttl_of(b)
                    if t:
                        candidates.append(t)
        for t in getattr(request, "tools", None) or []:
            ttl = _ttl_of(t)
            if ttl:
                candidates.append(ttl)
    except Exception:
        pass
    if "1h" in candidates:
        return "1h"
    if "5m" in candidates:
        return "5m"
    return default if default in ("5m", "1h") else "5m"


def beta_allows_1h(beta_header: Optional[str]) -> bool:
    """Check anthropic-beta header for the extended TTL feature."""
    if not beta_header:
        return False
    return EXTENDED_CACHE_TTL_BETA in str(beta_header)


def forwardable_beta(beta_header: Optional[str]) -> Optional[str]:
    """Allowlisted beta values to forward upstream (avoid header injection)."""
    if not beta_header:
        return None
    vals = [v.strip() for v in str(beta_header).split(",")]
    keep = [v for v in vals if v in (EXTENDED_CACHE_TTL_BETA,)]
    return ",".join(keep) if keep else None
