from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union, Literal

class ClaudeCacheControl(BaseModel):
    """P5: prompt-cache breakpoint. ttl "1h" needs extended-cache-ttl beta."""
    type: Literal["ephemeral"] = "ephemeral"
    ttl: Optional[str] = None

class ClaudeContentBlockText(BaseModel):
    type: Literal["text"]
    text: str
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeContentBlockImage(BaseModel):
    type: Literal["image"]
    source: Dict[str, Any]
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any]]
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeContentBlockThinking(BaseModel):
    """Thinking block echoed back in history (incl. proxy-emitted ones,
    which carry an empty signature). Accepted so history replay validates;
    converters intentionally drop thinking on the upstream path."""
    type: Literal["thinking"]
    thinking: str = ""
    signature: Optional[str] = None

class ClaudeContentBlockRedactedThinking(BaseModel):
    type: Literal["redacted_thinking"]
    data: str = ""
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeSystemContent(BaseModel):
    type: Literal["text"]
    text: str
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: Union[str, List[Union[ClaudeContentBlockText, ClaudeContentBlockImage, ClaudeContentBlockToolUse, ClaudeContentBlockToolResult, ClaudeContentBlockThinking, ClaudeContentBlockRedactedThinking]]]

class ClaudeTool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]
    cache_control: Optional[ClaudeCacheControl] = None

class ClaudeThinkingConfig(BaseModel):
    type: str = "enabled"
    budget_tokens: Optional[int] = None

class ClaudeMessagesRequest(BaseModel):
    model: str
    max_tokens: int
    messages: List[ClaudeMessage]
    system: Optional[Union[str, List[ClaudeSystemContent]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[ClaudeTool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[ClaudeThinkingConfig] = None

class ClaudeTokenCountRequest(BaseModel):
    model: str
    messages: List[ClaudeMessage]
    system: Optional[Union[str, List[ClaudeSystemContent]]] = None
    tools: Optional[List[ClaudeTool]] = None
    thinking: Optional[ClaudeThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
