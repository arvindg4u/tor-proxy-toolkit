"""Token estimator tests: accuracy behaviors of src.core.tokens.

Runs under pytest when available, and also as a plain script:
    python3 tests/test_token_estimator.py
(no third-party test deps required).
"""

import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.core.tokens import (  # noqa: E402
    IMAGE_TOKENS_ESTIMATE,
    estimate_input_tokens,
)


def test_empty_floor():
    assert estimate_input_tokens(messages=[]) >= 1
    assert estimate_input_tokens() >= 1


def test_scales_with_text():
    short = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}]
    )
    long = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi " * 500}]
    )
    assert long > short > 0


def test_system_and_tools_counted():
    base = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}]
    )
    with_system = estimate_input_tokens(
        system="you are helpful",
        messages=[{"role": "user", "content": "hi"}],
    )
    with_tools = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "name": "bash",
                "description": "run commands",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    assert with_system > base
    assert with_tools > base


def test_images_add_estimate():
    base = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}]
    )
    with_image = estimate_input_tokens(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBOR",
                        },
                    },
                ],
            }
        ],
    )
    assert with_image >= base + IMAGE_TOKENS_ESTIMATE


def test_thinking_budget_added():
    base = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}]
    )
    with_thinking = estimate_input_tokens(
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "enabled", "budget_tokens": 4000},
    )
    assert with_thinking >= base + 4000


_TESTS = [
    test_empty_floor,
    test_scales_with_text,
    test_system_and_tools_counted,
    test_images_add_estimate,
    test_thinking_budget_added,
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
