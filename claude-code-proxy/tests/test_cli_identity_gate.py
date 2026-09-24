"""Free-tier CLI-identity gate tests: genuine tool manifest, msg ids, de-stream.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_cli_identity_gate.py
(no third-party test deps required).
"""

import asyncio
import os
import re
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.api.endpoints import _collect_streamed_response  # noqa: E402
from src.conversion.request_responses import (  # noqa: E402
    convert_claude_to_responses,
)
from src.conversion.response_responses import (  # noqa: E402
    convert_responses_to_claude_response,
)
from src.core.cli_identity import (  # noqa: E402
    GENUINE_CLI_TOOLS,
    new_message_id,
    prepend_cli_preamble,
)
from src.core.config import config  # noqa: E402
from src.core.model_manager import model_manager  # noqa: E402
from src.models.claude import ClaudeMessagesRequest  # noqa: E402


class FakeRequest:
    async def is_disconnected(self):
        return False


class FakeResponsesClient:
    def __init__(self, events):
        self._events = list(events)
        self.cancelled = []

    def cancel_request(self, request_id):
        self.cancelled.append(request_id)
        return True

    async def create_response_stream(self, payload, request_id=None):
        assert payload.get("stream") is True, "upstream must always stream"
        for ev in self._events:
            yield ev


def _sse(event, data):
    import json

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _claude_req(**kw):
    base = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
    }
    base.update(kw)
    return ClaudeMessagesRequest(**base)


def test_toolless_turn_declares_genuine_tools():
    # Tightened gate (~2026-09-19): a turn with no tools at all gets
    # FreeTierError, so tool-less client turns still declare the full
    # genuine CLI manifest upstream (and carry NO title preamble, which
    # would hijack non-title prompts such as hook evaluation).
    out = convert_claude_to_responses(_claude_req(), model_manager)
    assert all(item.get("role") != "developer" for item in out["input"])
    assert out["input"][0]["role"] == "user"
    assert [t["name"] for t in out["tools"]] == [
        t["name"] for t in GENUINE_CLI_TOOLS
    ]
    assert "bash" in [t["name"] for t in out["tools"]]
    assert "read" in [t["name"] for t in out["tools"]]


def _claude_req_with_tools():
    return _claude_req(
        tools=[
            {
                "name": "Bash",
                "description": "execute shell commands",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        tool_choice={"type": "auto"},
    )


def test_no_preamble_with_tools():
    # Tool-carrying turns must NOT carry the title-dev preamble (the gate
    # rejects that combo); they carry the genuine tool manifest instead.
    out = convert_claude_to_responses(_claude_req_with_tools(), model_manager)
    assert all(item.get("role") != "developer" for item in out["input"])
    assert out["input"][0]["role"] == "user"


def test_genuine_tools_first():
    out = convert_claude_to_responses(_claude_req_with_tools(), model_manager)
    tools = out["tools"]
    # Full manifest (gate needs every name exactly) plus the caller tool.
    assert len(tools) == len(GENUINE_CLI_TOOLS) + 1
    assert [t["name"] for t in tools[: len(GENUINE_CLI_TOOLS)]] == [
        t["name"] for t in GENUINE_CLI_TOOLS
    ]
    # Caller's tool appended after the genuine manifest.
    assert tools[-1]["name"] == "Bash"
    assert out["tool_choice"] == "auto"


def test_manifest_duplicates_carry_caller_schema():
    # Read/Write intermittently failed: the model saw both manifest `read`
    # (opencode schema: `filePath`) and caller `Read` (Claude schema:
    # `file_path`), picked either at random, and manifest-shaped args broke
    # execution after the response-path rename to `Read`. Omitting the
    # manifest entry is NOT an option (the gate 403s without the exact
    # names — verified live). So the manifest entry keeps its name +
    # description but carries the caller's parameters: whichever variant
    # the model picks, args arrive executable. Shared manifest not mutated.
    caller_schema = {
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
    }
    req = _claude_req(
        tools=[
            {
                "name": "Read",
                "description": "read a file",
                "input_schema": caller_schema,
            },
            {
                "name": "Write",
                "description": "write a file",
                "input_schema": caller_schema,
            },
        ]
    )
    before = {t["name"]: t["parameters"] for t in GENUINE_CLI_TOOLS}
    out = convert_claude_to_responses(req, model_manager)
    names = [t["name"] for t in out["tools"]]
    # All 27 manifest names still present exactly (gate), callers appended.
    assert len(out["tools"]) == len(GENUINE_CLI_TOOLS) + 2
    assert "read" in names and "Read" in names
    assert "write" in names and "Write" in names
    by_name = {t["name"]: t for t in out["tools"]}
    # Manifest duplicates now speak the caller schema...
    assert "file_path" in by_name["read"]["parameters"]["properties"]
    assert "filePath" not in by_name["read"]["parameters"]["properties"]
    # ...names and descriptions untouched (gate-visible)...
    assert by_name["read"]["description"] == [
        t for t in GENUINE_CLI_TOOLS if t["name"] == "read"
    ][0]["description"]
    # ...untouched entries keep their own schema...
    assert by_name["bash"]["parameters"] == before["bash"]
    # ...and the shared manifest is not mutated.
    after = {t["name"]: t["parameters"] for t in GENUINE_CLI_TOOLS}
    assert before == after


def test_non_overlapping_caller_appends():
    req = _claude_req(
        tools=[
            {
                "name": "my_probe",
                "description": "probe",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
    )
    out = convert_claude_to_responses(req, model_manager)
    assert len(out["tools"]) == len(GENUINE_CLI_TOOLS) + 1
    assert out["tools"][-1]["name"] == "my_probe"


def test_preamble_not_duplicated():
    items = [{"role": "developer", "content": "x"}, {"role": "user"}]
    assert prepend_cli_preamble(items) == items
    assert prepend_cli_preamble([])[0]["role"] == "developer"


def test_upstream_headers_msg_id():
    h1 = config.get_upstream_headers()
    h2 = config.get_upstream_headers()
    for h in (h1, h2):
        assert re.fullmatch(r"msg_[A-Za-z0-9]{26}", h["x-opencode-request"]), h
        assert h["User-Agent"].startswith("opencode/1.18.")
    # Fresh id minted on every call.
    assert h1["x-opencode-request"] != h2["x-opencode-request"]
    assert re.fullmatch(r"msg_[A-Za-z0-9]{26}", new_message_id())


def test_collect_prefers_completed_payload():
    usage = {"input_tokens": 10, "output_tokens": 5}
    events = [
        _sse("response.output_text.delta", {"delta": "ignored-partial"}),
        _sse(
            "response.completed",
            {
                "response": {
                    "id": "resp_abc",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "hello"}],
                        }
                    ],
                    "usage": usage,
                }
            },
        ),
    ]
    client = FakeResponsesClient(events)
    obj = asyncio.run(
        _collect_streamed_response(client, {"stream": True}, "r1", FakeRequest())
    )
    assert obj["id"] == "resp_abc"
    assert obj["usage"] == usage
    claude = convert_responses_to_claude_response(obj, _claude_req())
    assert claude["content"][0] == {"type": "text", "text": "hello"}
    assert claude["stop_reason"] == "end_turn"


def test_collect_assembles_from_deltas():
    events = [
        _sse("response.output_text.delta", {"delta": "he"}),
        _sse("response.output_text.delta", {"delta": "llo"}),
    ]
    client = FakeResponsesClient(events)
    obj = asyncio.run(
        _collect_streamed_response(client, {"stream": True}, "r2", FakeRequest())
    )
    assert obj["status"] == "completed"
    claude = convert_responses_to_claude_response(obj, _claude_req())
    assert claude["content"][0] == {"type": "text", "text": "hello"}


_TESTS = [
    test_toolless_turn_declares_genuine_tools,
    test_preamble_not_duplicated,
    test_no_preamble_with_tools,
    test_genuine_tools_first,
    test_manifest_duplicates_carry_caller_schema,
    test_non_overlapping_caller_appends,
    test_upstream_headers_msg_id,
    test_collect_prefers_completed_payload,
    test_collect_assembles_from_deltas,
]


def main() -> int:
    failures = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        else:
            print(f"ok {fn.__name__}")
    print(f"{len(_TESTS) - failures}/{len(_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
