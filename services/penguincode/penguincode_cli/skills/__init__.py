"""Native skill system for PenguinCode.

Skills are markdown-based behavior guides that shape how the LLM processes
requests. They are compatible with OpenCode/Claude Code skill format.

Usage:
    from penguincode_cli.skills import SkillLoader, SkillInfo

    loader = SkillLoader()
    loader.discover()

    # List all available skills
    for name, info in loader.list_all().items():
        print(f"{name}: {info.description}")

    # Activate a skill
    skill = loader.get("brainstorming")
    if skill:
        agent.activate_skill(skill.name, skill.content)

    # Get a chain of skills (with dependencies)
    chain = loader.get_chain("brainstorming")
"""

from .loader import SkillInfo, SkillLoader

__all__ = ["SkillInfo", "SkillLoader"]
