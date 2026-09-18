"""Accurate input-token estimator for count_tokens (P4).

Replaces chars/4 with:
- full text extraction (system, messages, tool_use JSON, tool_result,
  thinking blocks, tool definitions + schemas)
- image token estimate (~1500 per image, Anthropic guideline)
- thinking budget overhead when enabled
- tiktoken when installed (mapped to upstream encoding), else
  improved heuristic (~3.6 chars/token for code-heavy text + overhead)
"""

import json
from typing import Any, Optional

IMAGE_TOKENS_ESTIMATE = 1500


def _block_text_len(block: Any) -> int:
    if isinstance(block, str):
        return len(block)
    if isinstance(block, dict):
        t = block.get("type")
        if t == "text":
            return len(str(block.get("text", "")))
        if t == "tool_use":
            try:
                return len(json.dumps(block.get("input", {}), ensure_ascii=False))
            except Exception:
                return len(str(block.get("input", "")))
        if t == "tool_result":
            c = block.get("content", "")
            if isinstance(c, str):
                return len(c)
            try:
                return len(json.dumps(c, ensure_ascii=False))
            except Exception:
                return len(str(c))
        if t in ("thinking", "redacted_thinking"):
            return len(str(block.get("thinking", "") or block.get("data", "")))
        if t == "image":
            return 0  # counted separately
        return len(str(block))
    if hasattr(block, "type"):
        btype = getattr(block, "type", "")
        if btype == "text":
            return len(str(getattr(block, "text", "") or ""))
        if btype == "image":
            return 0
        try:
            return len(json.dumps(getattr(block, "input", ""), ensure_ascii=False))
        except Exception:
            return 0
    return 0


def _count_images(content: Any) -> int:
    n = 0
    items = content if isinstance(content, list) else []
    for b in items:
        t = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if t == "image":
            n += 1
    return n


def extract_text(
    system: Any, messages: Any, tools: Any, thinking: Any
) -> tuple[str, int, int]:
    """Return (all_text, image_count) for counting."""
    parts: list[str] = []
    images = 0
    if system:
        if isinstance(system, str):
            parts.append(system)
        elif isinstance(system, list):
            for b in system:
                if isinstance(b, dict):
                    parts.append(str(b.get("text", "")))
                elif hasattr(b, "text"):
                    parts.append(str(getattr(b, "text", "") or ""))
                else:
                    parts.append(str(b))
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if content is None:
            continue
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            images += _count_images(content)
            for b in content:
                parts.append(_raw_block_text(b))
    if tools:
        for t in tools:
            if isinstance(t, dict):
                parts.append(str(t.get("name", "")))
                parts.append(str(t.get("description", "") or ""))
                try:
                    parts.append(json.dumps(t.get("input_schema", {}), ensure_ascii=False))
                except Exception:
                    pass
            else:
                parts.append(str(getattr(t, "name", "") or ""))
                parts.append(str(getattr(t, "description", "") or ""))
                try:
                    parts.append(
                        json.dumps(getattr(t, "input_schema", {}), ensure_ascii=False)
                    )
                except Exception:
                    pass
    thinking_budget = 0
    if thinking is not None:
        ttype = thinking.get("type") if isinstance(thinking, dict) else getattr(thinking, "type", None)
        if ttype == "enabled":
            budget = (
                thinking.get("budget_tokens")
                if isinstance(thinking, dict)
                else getattr(thinking, "budget_tokens", None)
            )
            thinking_budget = int(budget or 0)
    text = "\n".join(p for p in parts if p)
    return text, images, thinking_budget


def _raw_block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        t = block.get("type")
        if t == "text":
            return str(block.get("text", ""))
        if t == "tool_use":
            try:
                return json.dumps(block.get("input", {}), ensure_ascii=False)
            except Exception:
                return str(block.get("input", ""))
        if t == "tool_result":
            c = block.get("content", "")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
        if t == "thinking":
            return str(block.get("thinking", ""))
        if t == "redacted_thinking":
            return str(block.get("data", ""))
        if t == "image":
            return ""
        return ""
    if hasattr(block, "type"):
        btype = getattr(block, "type", "")
        if btype == "text":
            return str(getattr(block, "text", "") or "")
        return ""
    return ""


def _tiktoken_count(text: str, model: Optional[str]) -> Optional[int]:
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None
    try:
        m = (model or "").lower()
        enc_name = "cl100k_base"
        try:
            if "gpt-4o" in m or "gpt-4" in m:
                enc = tiktoken.encoding_for_model("gpt-4o")
            else:
                enc = tiktoken.get_encoding(enc_name)
        except Exception:
            enc = tiktoken.get_encoding(enc_name)
        return len(enc.encode(text))
    except Exception:
        return None


def estimate_input_tokens(
    model: Optional[str] = None,
    system: Any = None,
    messages: Any = None,
    tools: Any = None,
    thinking: Any = None,
) -> int:
    """Estimate input tokens incl. overhead (tools/images/thinking)."""
    text, images, thinking_budget = extract_text(system, messages, tools, thinking)
    base = _tiktoken_count(text, model)
    if base is None:
        # Improved heuristic: ~3.6 chars/token for mixed code/prose
        # (chars/4 undercounts code/non-English by 20-50%).
        base = int(len(text) / 3.6) if text else 0
    # Per-message + tool overhead (Anthropic format tokens, JSON keys).
    n_msgs = len(messages or [])
    n_tools = len(tools or [])
    overhead = n_msgs * 4 + n_tools * 20
    total = base + overhead + images * IMAGE_TOKENS_ESTIMATE
    if thinking_budget:
        total += thinking_budget
    return max(1, int(total))
