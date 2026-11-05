"""Procedural skill bodies carry compact, optional resource-directory guidance."""

from pathlib import Path

import pytest

from kohakuterrarium.skills.registry import Skill


@pytest.mark.parametrize(
    "body", ["", "Read references/YYY.md.\n\n", "Use ./src in the project."]
)
def test_body_without_directory_is_unchanged(body):
    skill = Skill("review", "Review", body)
    assert skill.render_body() == body


def test_directory_guidance_preserves_body_and_project_path_meaning(tmp_path):
    body = "Read references/YYY.md. Project paths are relative to the workspace.\n"
    skill = Skill("review", "Review", body, base_dir=tmp_path / "技能 with spaces")
    result = skill.render_body()
    assert result.endswith(body)
    assert result.count(str(skill.base_dir)) == 1
    assert "absolute paths" in result
    assert "explicit project-relative paths" in result


def test_two_skills_keep_their_own_directory(tmp_path):
    first = Skill("first", "First", "Read YYY.md.", base_dir=tmp_path / "one")
    second = Skill("second", "Second", "Read YYY.md.", base_dir=tmp_path / "two")
    assert str(first.base_dir) in first.render_body()
    assert str(second.base_dir) not in first.render_body()
    assert str(second.base_dir) in second.render_body()
    assert str(first.base_dir) not in second.render_body()


def test_empty_body_still_exposes_known_directory():
    skill = Skill("empty", "Empty", "", base_dir=Path.cwd())
    assert str(skill.base_dir) in skill.render_body()
