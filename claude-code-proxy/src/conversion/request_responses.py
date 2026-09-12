"""Convert Claude Messages API requests to OpenAI Responses API requests.

Used when UPSTREAM_WIRE_API=responses. Some free-tier upstreams (e.g. Muse
Spark on OpenCode ZEN) only serve the Responses endpoint and fail on
/chat/completions, so the proxy translates Claude -> Responses instead of
Claude -> Chat Completions on this path.
"""

import json
import logging
from typing import Any, Dict, List

from src.core.constants import Constants
from src.core.config import config
from src.models.claude import ClaudeMessagesRequest, ClaudeMessage

logger = logging.getLogger(__name__)


def convert_claude_to_responses(
    claude_request: ClaudeMessagesRequest, model_manager
) -> Dict[str, Any]:
    """Convert Claude API request format to Responses API format."""

    # Map model (same BIG/MIDDLE/SMALL mapping as the chat path)
    model = model_manager.map_claude_model_to_openai(claude_request.model)

    responses_request: Dict[str, Any] = {"model": model}

    # System prompt -> instructions
    instructions = _extract_system_text(claude_request.system)
    if instructions:
        responses_request["instructions"] = instructions

    # Messages -> input items
    responses_request["input"] = _convert_messages(claude_request.messages)

    # Token budget: clamp like the chat path, then apply the responses floor
    # (reasoning models consume output tokens before emitting visible text).
    budget = min(
        max(claude_request.max_tokens, config.min_tokens_limit),
        config.max_tokens_limit,
    )
    responses_request["max_output_tokens"] = max(
        budget, config.responses_min_output_tokens
    )

    # Sampling params (verified accepted by ZEN/Muse Spark)
    if claude_request.temperature is not None:
        responses_request["temperature"] = claude_request.temperature
    if claude_request.top_p is not None:
        responses_request["top_p"] = claude_request.top_p

    responses_request["stream"] = bool(claude_request.stream)

    # Thinking -> reasoning effort (Muse Spark accepts low/medium/high/xhigh)
    effort = _map_thinking_to_effort(claude_request)
    if effort is not None:
        responses_request["reasoning"] = {"effort": effort}

    # Tools: flatten to function tools (ZEN/Muse Spark rejects
    # custom/namespace tool types — same fix as mimo2codex).
    if claude_request.tools:
        responses_tools = []
        for tool in claude_request.tools:
            if tool.name and tool.name.strip():
                parameters = tool.input_schema or {}
                if not isinstance(parameters, dict):
                    parameters = {}
                if not parameters:
                    parameters = {"type": "object", "properties": {}}
                responses_tools.append(
                    {
                        "type": Constants.TOOL_FUNCTION,
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": parameters,
                    }
                )
        if responses_tools:
            responses_request["tools"] = responses_tools

    # Tool choice
    # Tool choice: this upstream accepts ONLY "auto" — "none",
    # "required" and named-function choices all 400 with
    # param=tool_choice. So everything maps to "auto" (forcing/no-tools
    # semantics cannot be expressed; sending them would hard-fail the
    # request instead).
    if claude_request.tool_choice:
        responses_request["tool_choice"] = "auto"

    logger.debug(
        "Converted Claude request to Responses format: %s",
        json.dumps(responses_request, indent=2, ensure_ascii=False),
    )
    return responses_request


def _extract_system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system.strip()
    if isinstance(system, list):
        parts = []
        for block in system:
            if hasattr(block, "type") and block.type == Constants.CONTENT_TEXT:
                parts.append(block.text)
            elif isinstance(block, dict) and block.get("type") == Constants.CONTENT_TEXT:
                parts.append(block.get("text", ""))
        return "\n\n".join(parts).strip()
    return ""


def _map_thinking_to_effort(claude_request: ClaudeMessagesRequest):
    thinking = claude_request.thinking
    if not thinking:
        return None
    if thinking.type == "disabled":
        return "low"
    if thinking.type == "enabled":
        budget = thinking.budget_tokens or 0
        if budget >= 16000:
            return "xhigh"
        if budget >= 4000:
            return "high"
        if budget > 0:
            return "medium"
        return "high"
    return None


def _convert_messages(messages: List[ClaudeMessage]) -> List[Dict[str, Any]]:
    """Convert Claude messages to Responses input items."""
    items: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.role == Constants.ROLE_SYSTEM:
            text = _blocks_to_text(msg.content)
            if text:
                items.append(_user_text_item(f"[System instruction]: {text}"))
        elif msg.role == Constants.ROLE_USER:
            items.extend(_convert_user_message(msg))
        elif msg.role == Constants.ROLE_ASSISTANT:
            items.extend(_convert_assistant_message(msg))
    return items


def _blocks_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if hasattr(block, "type") and block.type == Constants.CONTENT_TEXT:
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == Constants.CONTENT_TEXT:
            parts.append(block.get("text", ""))
    return "".join(parts)


def _user_text_item(text: str) -> Dict[str, Any]:
    return {
        "type": "message",
        "role": Constants.ROLE_USER,
        "content": [{"type": "input_text", "text": text}],
    }


def _image_block_to_input_part(block) -> Dict[str, Any] | None:
    """Convert a Claude image content block to a Responses input part.

    Accepts both pydantic blocks and plain dicts. Returns None when the
    block carries no usable image payload.
    """
    source = block.source if hasattr(block, "source") else block.get("source", {})
    if not isinstance(source, dict):
        return None
    stype = source.get("type")
    if stype == "base64":
        data = source.get("data", "")
        if not data:
            return None
        media_type = source.get("media_type") or _sniff_media_type(data)
        return {
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{data}",
        }
    if stype == "url":
        url = source.get("url", "")
        if not url:
            return None
        return {"type": "input_image", "image_url": url}
    return None


def _sniff_media_type(b64: str) -> str:
    """Best-effort media type for base64 image data missing `media_type`.

    Claude clients sometimes send image blocks without `media_type`
    (seen in tool_result payloads); the upstream rejects the request
    when the data URL has no usable MIME type, so sniff the magic bytes.
    """
    head = (b64 or "").lstrip()[:16]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("iVBOR"):
        return "image/png"
    if head.startswith("R0lGOD"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/jpeg"


def _tool_result_text_and_images(raw) -> tuple[str, List[Dict[str, Any]]]:
    """Split tool_result content into text plus image input parts.

    Image blocks inside tool results must NOT be JSON-dumped into the
    `function_call_output.output` string (the upstream rejects that with
    `invalid_request_error`); they become `input_image` parts instead.
    """
    texts: List[str] = []
    images: List[Dict[str, Any]] = []
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            try:
                texts.append(json.dumps(item, ensure_ascii=False))
            except Exception:
                texts.append(str(item))
            continue
        itype = item.get("type")
        if itype == Constants.CONTENT_IMAGE:
            part = _image_block_to_input_part(item)
            if part is not None:
                images.append(part)
        elif itype == Constants.CONTENT_TEXT:
            texts.append(item.get("text", ""))
        elif "text" in item:
            texts.append(item.get("text", ""))
        else:
            try:
                texts.append(json.dumps(item, ensure_ascii=False))
            except Exception:
                texts.append(str(item))
    return "\n".join(texts).strip(), images


def _convert_user_message(msg: ClaudeMessage) -> List[Dict[str, Any]]:
    """User message -> user message item + function_call_output items.

    Outputs are ALWAYS emitted before any text item. The upstream provider
    rejects (400/500) a `message` item placed between a `function_call` and
    its `function_call_output`, which is exactly what happens during
    compaction when a tool_result message also carries text.
    """
    items: List[Dict[str, Any]] = []
    content = msg.content
    if content is None:
        return [_user_text_item("")]
    if isinstance(content, str):
        return [_user_text_item(content)]

    text_parts: List[Dict[str, Any]] = []
    pending_images: List[Dict[str, Any]] = []
    for block in content:
        btype = block.type if hasattr(block, "type") else block.get("type")
        if btype == Constants.CONTENT_TEXT:
            text = block.text if hasattr(block, "text") else block.get("text", "")
            text_parts.append({"type": "input_text", "text": text})
        elif btype == Constants.CONTENT_IMAGE:
            part = _image_block_to_input_part(block)
            if part is not None:
                text_parts.append(part)
        elif btype == Constants.CONTENT_TOOL_RESULT:
            tool_use_id = (
                block.tool_use_id
                if hasattr(block, "tool_use_id")
                else block.get("tool_use_id", "")
            )
            raw = block.content if hasattr(block, "content") else block.get("content")
            text, images = _tool_result_text_and_images(raw)
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_use_id,
                    "output": text or "No content provided",
                }
            )
            pending_images.extend(images)
    if text_parts:
        items.append(
            {
                "type": "message",
                "role": Constants.ROLE_USER,
                "content": text_parts,
            }
        )
    if pending_images:
        # Images returned inside tool results ride along as their own
        # message item right after the outputs (never inside the
        # output string, and never between a function_call and its
        # output — outputs were already emitted above).
        items.append(
            {
                "type": "message",
                "role": Constants.ROLE_USER,
                "content": pending_images,
            }
        )
    if not items:
        # Upstream rejects `input: []` with invalid_request_error, so an
        # image-only message whose image was unusable still sends text.
        items.append(_user_text_item(""))
    return items


def _convert_assistant_message(msg: ClaudeMessage) -> List[Dict[str, Any]]:
    """Assistant message -> assistant message item + function_call items."""
    items: List[Dict[str, Any]] = []
    content = msg.content
    if content is None:
        return items
    if isinstance(content, str):
        if content:
            items.append(
                {
                    "type": "message",
                    "role": Constants.ROLE_ASSISTANT,
                    "content": [{"type": "output_text", "text": content}],
                }
            )
        return items

    text = ""
    for block in content:
        btype = block.type if hasattr(block, "type") else block.get("type")
        if btype == Constants.CONTENT_TEXT:
            text += block.text if hasattr(block, "text") else block.get("text", "")
        elif btype == Constants.CONTENT_TOOL_USE:
            bid = block.id if hasattr(block, "id") else block.get("id", "")
            name = block.name if hasattr(block, "name") else block.get("name", "")
            binput = block.input if hasattr(block, "input") else block.get("input", {})
            # Pass the Claude tool_use id through as call_id so the next
            # turn's tool_result maps back without an id translation table.
            items.append(
                {
                    "type": "function_call",
                    "call_id": bid,
                    "name": name,
                    "arguments": json.dumps(binput or {}, ensure_ascii=False),
                }
            )
    if text:
        items.insert(
            0,
            {
                "type": "message",
                "role": Constants.ROLE_ASSISTANT,
                "content": [{"type": "output_text", "text": text}],
            },
        )
    return items
