"""Per-1M-token list-price table + cost helper (P1).

Matches Claude Code's client-side estimate approach: list price, no
residency multiplier. Upstream is OpenAI-compatible but cost is shown
in Claude terms so it lines up with /cost expectations.
"""

from typing import Dict, Optional

# Prices per 1M tokens. P4: cache creation split into 5m (1.25x input)
# and 1h (2x input) tiers; cache hits 0.1x. Default TTL is 5m (P5 adds
# 1h passthrough). Keep legacy "cache_write" (=5m) for back-compat.
_PRICES: Dict[str, Dict[str, float]] = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.50, "cache_write": 18.75, "cache_write_5m": 18.75, "cache_write_1h": 30.0},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75, "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "haiku": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_write": 1.00, "cache_write_5m": 1.00, "cache_write_1h": 1.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_write": 0.15, "cache_write_5m": 0.15, "cache_write_1h": 0.30},
    "gpt-4o": {"input": 2.50, "output": 10.0, "cache_read": 1.25, "cache_write": 2.50, "cache_write_5m": 2.50, "cache_write_1h": 5.00},
    "default": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75, "cache_write_5m": 3.75, "cache_write_1h": 6.0},
}


def price_for_model(model: Optional[str]) -> Dict[str, float]:
    """Pick a price row by substring match (Claude family first)."""
    m = (model or "").lower()
    if "opus" in m:
        return _PRICES["opus"]
    if "sonnet" in m:
        return _PRICES["sonnet"]
    if "haiku" in m:
        return _PRICES["haiku"]
    if "gpt-4o-mini" in m or "mini" in m:
        return _PRICES["gpt-4o-mini"]
    if "gpt-4o" in m or "gpt-4" in m:
        return _PRICES["gpt-4o"]
    return _PRICES["default"]


def _norm_usage(usage: Optional[Dict]) -> Dict[str, int]:
    if not usage:
        return {"in": 0, "out": 0, "read": 0, "write": 0}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    comp = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    read = int(
        usage.get("cache_read_input_tokens", 0)
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        or (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)
    )
    write = int(usage.get("cache_creation_input_tokens", 0))
    return {"in": prompt, "out": comp, "read": read, "write": write}


def cost_usd(
    model: Optional[str], usage: Optional[Dict], ttl: str = "5m"
) -> float:
    """USD cost for one completed response's usage block.

    ttl selects the cache-creation tier ("5m" default, "1h" when the
    request asked for extended TTL - full passthrough lands in P5).
    """
    u = _norm_usage(usage)
    if u["in"] == 0 and u["out"] == 0 and u["read"] == 0 and u["write"] == 0:
        return 0.0
    p = price_for_model(model)
    write_key = "cache_write_1h" if ttl == "1h" else "cache_write_5m"
    write_price = p.get(write_key, p.get("cache_write", p["input"] * 1.25))
    return (
        u["in"] * p["input"] / 1_000_000
        + u["out"] * p["output"] / 1_000_000
        + u["read"] * p["cache_read"] / 1_000_000
        + u["write"] * write_price / 1_000_000
    )
