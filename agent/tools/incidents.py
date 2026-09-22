"""Tools over `agent/incidents.py`'s structured incident memory.

Same per-call `Store` pattern as `agent/tools/usage.py` and the `Brain`
construction in `agent/tools/memory.py`: no dependency is injected into a
tool function, so each one builds what it needs from the same env vars
`agent/config.py` defaults.
"""

import os
from typing import Any

from agent import incidents
from agent.store import Store
from agent.tools.base import tool

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20


def _store() -> Store:
    return Store(os.environ.get("AGENT_DB", "/tank/dev/agent/agent.db"))


@tool(
    "incident_find",
    "Look for a past incident that resembles a problem you're diagnosing right now, "
    "BEFORE you start diagnosing from scratch. `component` is the short name of what's "
    "affected (e.g. 'hostctl', 'jellyfin', 'mail', 'zfs') - matched exactly against "
    "past incidents' own component field. `symptom` is a short description of what "
    "you're observing right now, in your own words - matching is by meaning-ish word "
    "overlap plus a same-component bonus, not exact text, so a paraphrase of an old "
    "symptom can still match. Each result includes a `score` (higher is a closer "
    "match) and the incident's recorded cause and fix. An empty result means no past "
    "incident matched well enough to be useful - that's a real answer, not a failure.",
    {
        "type": "object",
        "properties": {
            "component": {"type": "string", "minLength": 1},
            "symptom": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
        },
        "required": ["component", "symptom"],
        "additionalProperties": False,
    },
)
def incident_find(component: str, symptom: str, limit: int = _DEFAULT_LIMIT) -> dict[str, Any]:
    limit = min(max(limit, 1), _MAX_LIMIT)
    matches = incidents.find_similar(_store(), component=component, symptom=symptom, limit=limit)
    return {
        "matches": [
            {"score": round(m.score, 3), **m.incident} for m in matches
        ]
    }


@tool(
    "incident_record",
    "Record a resolved incident so the next occurrence isn't diagnosed from scratch: "
    "what was observed (`symptom`), what it turned out to be (`cause`), and what fixed "
    "it (`fix`), tagged with the affected `component`. Call this after you actually fix "
    "something concrete - not for every tool call, just faults worth remembering. Call "
    "incident_find first with the same component/symptom before diagnosing; if nothing "
    "matched, this is the record that will let a future incident_find find THIS one.",
    {
        "type": "object",
        "properties": {
            "component": {"type": "string", "minLength": 1},
            "symptom": {"type": "string", "minLength": 1},
            "cause": {"type": "string", "minLength": 1},
            "fix": {"type": "string", "minLength": 1},
        },
        "required": ["component", "symptom", "cause", "fix"],
        "additionalProperties": False,
    },
)
def incident_record(component: str, symptom: str, cause: str, fix: str) -> dict[str, Any]:
    return incidents.record(_store(), component=component, symptom=symptom, cause=cause, fix=fix)
