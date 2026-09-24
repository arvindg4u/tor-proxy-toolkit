"""Canonical tool-name normalization tests: lowercase upstream echo.

Regression test for Claude Code CLI errors like:
    Error: No such tool available: grep / glob / todowrite / bash / skill

Root cause: the proxy prepends the genuine 27-tool OpenCode CLI manifest
(lowercase names: `grep`, `glob`, `todowrite`, ...) to satisfy the ZEN
free-tier gate. When the upstream model echoes one of those lowercase
manifest names in a function_call instead of the caller's PascalCase tool
(e.g. `Grep`), the proxy forwarded it verbatim and Claude Code rejected it.

Fix: `from_upstream_name()` normalizes known lowercase names to their
canonical Claude-side PascalCase form, in addition to the 64-char alias
reverse lookup.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_canonical_tool_names.py
(no third-party test deps required).
"""

import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversion.tool_names import (  # noqa: E402
    from_upstream_name,
    to_upstream_name,
)


def test_lowercase_builtins_normalized():
    cases = {
        "bash": "Bash",
        "read": "Read",
        "edit": "Edit",
        "write": "Write",
        "glob": "Glob",
        "grep": "Grep",
        "todowrite": "TodoWrite",
        "task": "Task",
        "skill": "Skill",
        "webfetch": "WebFetch",
        "websearch": "WebSearch",
    }
    for given, expected in cases.items():
        assert from_upstream_name(given) == expected, given


def test_canonical_names_untouched():
    for name in ("Bash", "Grep", "TodoWrite", "Task", "Read", "Glob"):
        assert from_upstream_name(name) == name, name
        # Request path must keep passing valid names through as-is.
        assert to_upstream_name(name) == name, name


def test_unknown_names_untouched():
    # OpenCode-specific manifest tools have no Claude equivalent:
    # they must pass through unchanged (never silently remapped).
    for name in ("changed-files", "lint-check", "git-summary", "search"):
        assert from_upstream_name(name) == name, name


def test_alias_lookup_still_wins():
    long_name = (
        "mcp__plugin_microsoft-docs_microsoft-learn__microsoft_code_sample_search"
    )
    alias = to_upstream_name(long_name)
    assert alias != long_name
    assert from_upstream_name(alias) == long_name


if __name__ == "__main__":
    test_lowercase_builtins_normalized()
    print("ok test_lowercase_builtins_normalized")
    test_canonical_names_untouched()
    print("ok test_canonical_names_untouched")
    test_unknown_names_untouched()
    print("ok test_unknown_names_untouched")
    test_alias_lookup_still_wins()
    print("ok test_alias_lookup_still_wins")
    print("test_canonical_tool_names: all 4 tests passed")
