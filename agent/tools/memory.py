import os
from typing import Any

from agent.brain import TOPIC_PATTERN, Brain
from agent.tools.base import tool


def _brain() -> Brain:
    return Brain(os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md"))


@tool(
    "brain_read",
    "Read what you previously recorded about a topic, for example a recurring fault.",
    {
        "type": "object",
        "properties": {"topic": {"type": "string", "pattern": TOPIC_PATTERN.pattern}},
        "required": ["topic"],
        "additionalProperties": False,
    },
)
def brain_read(topic: str) -> dict[str, Any]:
    return {"topic": topic, "content": _brain().read(topic)}


@tool(
    "brain_write",
    "Record something worth remembering across restarts. Replaces the topic's previous note.",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "pattern": TOPIC_PATTERN.pattern},
            "content": {"type": "string"},
        },
        "required": ["topic", "content"],
        "additionalProperties": False,
    },
)
def brain_write(topic: str, content: str) -> dict[str, Any]:
    _brain().write(topic, content)
    return {"topic": topic, "saved": True}
