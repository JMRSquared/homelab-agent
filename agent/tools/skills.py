from typing import Any

from agent import skills
from agent.tools.base import tool


@tool(
    "skill_list",
    "List every skill (owner-written runbook for this homelab) by name and one-line "
    "description. Your system prompt already carries this list; call this only to "
    "pick up a skill added since you started.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def skill_list() -> dict[str, Any]:
    return {
        "skills": [
            {"name": s.name, "description": s.description}
            for s in sorted(skills.load_all().values(), key=lambda s: s.name)
        ]
    }


@tool(
    "skill_read",
    "Read one skill in full by name: the runbook for a service or task on this "
    "homelab - where it lives, how to check it, known faults and their fixes. Call it "
    "before acting on anything a skill covers.",
    {
        "type": "object",
        "properties": {"name": {"type": "string", "pattern": skills.NAME_PATTERN.pattern}},
        "required": ["name"],
        "additionalProperties": False,
    },
)
def skill_read(name: str) -> dict[str, Any]:
    skill = skills.get(name)
    if skill is None:
        available = sorted(skills.load_all())
        raise KeyError(f"no skill named {name!r}; available: {', '.join(available)}")
    return {"name": skill.name, "description": skill.description, "content": skill.body}
