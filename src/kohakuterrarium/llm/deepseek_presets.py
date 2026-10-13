"""DeepSeek direct Chat Completions presets with context and reasoning options."""

from typing import Any

_CONTEXT_GROUP: dict[str, dict[str, Any]] = {
    "272k": {"max_context": 272000},
    "400k": {"max_context": 400000},
    "700k": {"max_context": 700000},
    "1m": {"max_context": 1048576},
}

_REASONING_GROUP: dict[str, dict[str, Any]] = {
    "none": {
        "extra_body.thinking.type": "disabled",
        "extra_body.reasoning_effort": "none",
    },
    **{
        effort: {
            "extra_body.thinking.type": "enabled",
            "extra_body.reasoning_effort": effort,
        }
        for effort in ("low", "high", "max")
    },
}

PRESETS: dict[str, dict[str, Any]] = {
    model: {
        "provider": "deepseek",
        "model": model,
        "max_context": 1048576,
        "max_output": 393216,
        "extra_body": {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
        "variation_groups": {
            "context": _CONTEXT_GROUP,
            "reasoning": _REASONING_GROUP,
        },
    }
    for model in ("deepseek-flash", "deepseek-v4-pro")
}
