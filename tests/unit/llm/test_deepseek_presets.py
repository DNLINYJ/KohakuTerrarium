"""DeepSeek catalog metadata and protocol-specific reasoning variations."""

import pytest

from kohakuterrarium.llm.deepseek_presets import PRESETS
from kohakuterrarium.llm.profile_types import LLMPreset
from kohakuterrarium.llm.variations import (
    apply_variation_groups,
    normalize_variation_selections,
)


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_official_model_metadata_and_defaults(model):
    preset = PRESETS[model]
    assert preset["provider"] == "deepseek"
    assert preset["model"] == model
    assert preset["max_context"] == 1048576
    assert preset["max_output"] == 393216
    assert preset["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert set(preset["variation_groups"]) == {"reasoning"}
    assert set(preset["variation_groups"]["reasoning"]) == {
        "none",
        "low",
        "high",
        "max",
    }


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_reasoning_variants_use_chat_completions_fields(model, effort):
    preset = LLMPreset.from_dict(model, PRESETS[model])
    selections = normalize_variation_selections({"reasoning": effort}, preset)
    selected = apply_variation_groups(
        preset.to_dict(), preset.variation_groups, selections
    )
    assert selected["extra_body"] == {
        "thinking": {"type": "disabled" if effort == "none" else "enabled"},
        "reasoning_effort": effort,
    }
    assert preset.extra_body["reasoning_effort"] == "high"
    assert preset.extra_body["thinking"]["type"] == "enabled"


@pytest.mark.parametrize("effort", ["medium", "xhigh", "ultra", "invalid"])
def test_only_native_effort_levels_are_exposed(effort):
    preset = LLMPreset.from_dict("deepseek-flash", PRESETS["deepseek-flash"])
    with pytest.raises(ValueError, match=effort):
        normalize_variation_selections({"reasoning": effort}, preset)


def test_retired_model_names_are_not_separate_catalog_entries():
    assert set(PRESETS) == {"deepseek-flash", "deepseek-v4-pro"}
