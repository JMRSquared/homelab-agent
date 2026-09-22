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


@tool(
    "brain_list",
    "List every topic currently recorded in the brain, in full. Read this before "
    "calling brain_consolidate - you decide what should merge, go stale, or "
    "contradict something else; this just shows you everything on file at once "
    "instead of one topic at a time.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def brain_list() -> dict[str, Any]:
    return {"topics": _brain().topics()}


@tool(
    "brain_consolidate",
    "Safely merge, prune, or flag contradictions in the brain - call this "
    "occasionally, not on every improvement cycle, and only after reading the "
    "current state with brain_list and deciding the merge yourself. `updates` is a "
    "map of topic -> new content: each topic listed is fully replaced with the "
    "content you give (the same as brain_write, but batched into one safe, "
    "recoverable step). `drop` permanently removes topics that are stale or fully "
    "superseded by something in `updates`. `contradictions` is a list of short notes "
    "about things that disagree with each other in the brain - each is appended to a "
    "dedicated 'Contradictions' topic rather than resolved for you, so a human (or a "
    "more careful future pass) can look at both versions instead of one being "
    "silently picked. This backs up the whole brain file before touching it - the "
    "result's backup_path says where - so a bad merge is always recoverable.",
    {
        "type": "object",
        "properties": {
            "updates": {"type": "object", "additionalProperties": {"type": "string"}},
            "drop": {"type": "array", "items": {"type": "string"}},
            "contradictions": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
)
def brain_consolidate(
    updates: dict[str, str] | None = None,
    drop: list[str] | None = None,
    contradictions: list[str] | None = None,
) -> dict[str, Any]:
    result = _brain().consolidate(updates=updates, drop=drop, contradictions=contradictions)
    return {
        "backup_path": result.backup_path,
        "updated_topics": result.updated_topics,
        "dropped_topics": result.dropped_topics,
        "contradictions_recorded": result.contradictions_recorded,
    }
