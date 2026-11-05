"""Discovered resource directories remain anchored after working-directory changes."""

from pathlib import Path

import pytest

from kohakuterrarium.skills.discovery import load_skill_from_path


@pytest.mark.parametrize(
    "relative_file", ["skills/review/SKILL.md", "skills/review.md"]
)
def test_relative_source_keeps_original_resource_directory(
    tmp_path, monkeypatch, relative_file
):
    source = tmp_path / relative_file
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: review\n---\nRead YYY.md.\n", encoding="utf-8")
    (source.parent / "YYY.md").write_text("skill resource", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    skill = load_skill_from_path(Path(relative_file), origin="user")
    elsewhere = tmp_path / "other-project"
    elsewhere.mkdir()
    (elsewhere / "YYY.md").write_text("wrong resource", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert skill.base_dir == source.parent.resolve()
    assert (skill.base_dir / "YYY.md").read_text(encoding="utf-8") == "skill resource"
