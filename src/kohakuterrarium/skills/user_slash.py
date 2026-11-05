"""Build user-turn content for ``/<skill-name>`` invocations."""

from kohakuterrarium.skills.registry import Skill


def build_user_skill_turn(skill: Skill, arguments: str) -> str:
    """Render skill instructions and append non-empty user arguments."""
    header = f'Please follow the "{skill.name}" skill:\n\n'
    body = skill.render_body().rstrip()
    arg_line = f"\n\nArguments the user provided: {arguments}" if arguments else ""
    return f"{header}{body}{arg_line}"
