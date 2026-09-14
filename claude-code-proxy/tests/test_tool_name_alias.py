"""Tool-name aliasing tests: upstream 64-char `name` limit.

Regression test for the upstream 400:
    [invalid_request_error] `name` must be at most 64 characters, got 72
triggered by MCP tool names such as
`mcp__plugin_microsoft-docs_microsoft-learn__microsoft_code_sample_search`
(72 chars), which the proxy used to forward verbatim.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_tool_name_alias.py
(no third-party test deps required).
"""

import os
import re
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversion.request_responses import (  # noqa: E402
    _convert_assistant_message,
    convert_claude_to_responses,
)
from src.conversion.tool_names import (  # noqa: E402
    MAX_TOOL_NAME_LEN,
    from_upstream_name,
    to_upstream_name,
)
from src.models.claude import (  # noqa: E402
    ClaudeMessage,
    ClaudeMessagesRequest,
    ClaudeTool,
)

LONG_SEARCH = "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_docs_search"
LONG_CODE = "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_code_sample_search"
LONG_FETCH = "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_docs_fetch"
assert len(LONG_CODE) == 72, len(LONG_CODE)
assert len(LONG_SEARCH) == 65, len(LONG_SEARCH)
assert len(LONG_FETCH) == 64, len(LONG_FETCH)

VALID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class StubModelManager:
    @staticmethod
    def map_claude_model_to_openai(model):
        return "test-model"


def _request_with_tools(names):
    return ClaudeMessagesRequest(
        model="claude-opus-4-8",
        max_tokens=100,
        messages=[ClaudeMessage(role="user", content="hi")],
        tools=[
            ClaudeTool(
                name=n,
                description="d",
                input_schema={"type": "object", "properties": {}},
            )
            for n in names
        ],
    )


def test_valid_names_pass_through():
    assert to_upstream_name("search") == "search"
    assert to_upstream_name(LONG_FETCH) == LONG_FETCH
    assert from_upstream_name("search") == "search"


def test_long_names_aliased_within_limit():
    for original in (LONG_CODE, LONG_SEARCH):
        alias = to_upstream_name(original)
        assert alias != original, original
        assert len(alias) <= MAX_TOOL_NAME_LEN, (original, alias)
        assert VALID.match(alias), alias
        # Deterministic: same input, same alias (stable across restarts,
        # so in-flight streams survive proxy restarts mid-turn).
        assert to_upstream_name(original) == alias


def test_round_trip():
    for original in (LONG_CODE, LONG_SEARCH):
        alias = to_upstream_name(original)
        assert from_upstream_name(alias) == original


def test_responses_request_uses_aliases():
    req = _request_with_tools(["search", LONG_CODE, LONG_SEARCH])
    out = convert_claude_to_responses(req, StubModelManager())
    names = [t["name"] for t in out["tools"]]
    assert "search" in names
    assert LONG_CODE not in names and LONG_SEARCH not in names
    assert all(len(n) <= MAX_TOOL_NAME_LEN and VALID.match(n) for n in names), names
    # Upstream echo resolves back to the Claude-side names.
    assert {from_upstream_name(n) for n in names} == {
        "search",
        LONG_CODE,
        LONG_SEARCH,
    }


def test_history_replay_uses_alias():
    msg = ClaudeMessage(
        role="assistant",
        content=[
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": LONG_CODE,
                "input": {"query": "x"},
            }
        ],
    )
    items = _convert_assistant_message(msg)
    calls = [i for i in items if i.get("type") == "function_call"]
    assert len(calls) == 1
    assert calls[0]["name"] == to_upstream_name(LONG_CODE)
    assert len(calls[0]["name"]) <= MAX_TOOL_NAME_LEN


if __name__ == "__main__":
    test_valid_names_pass_through()
    test_long_names_aliased_within_limit()
    test_round_trip()
    test_responses_request_uses_aliases()
    test_history_replay_uses_alias()
    print("test_tool_name_alias: all 5 tests passed")
