"""Expose `agent/usage.py`'s token-accounting summary as a tool, so the
agent can answer "how much have you been using?" from Slack.

Builds its own short-lived `Store` from the same `AGENT_DB` env var
`agent/config.py` defaults, the same pattern `agent/tools/memory.py` uses
for `Brain` - tool functions take no injected dependencies, so each one
that needs the database opens its own connection.
"""

import os
from typing import Any

from agent import usage
from agent.store import Store
from agent.tools.base import tool


def _store() -> Store:
    return Store(os.environ.get("AGENT_DB", "/tank/dev/agent/agent.db"))


@tool(
    "usage_report",
    "Report model token usage for today and the trailing 7 days, broken down by "
    "what drove it (which loop or conversation - the tick, the improvement cycle, "
    "Slack chat) and by model. Built from the token counts the model API returned "
    "with each response, not a bill - the result says so plainly; the provider's "
    "own usage dashboard is the authority on what was actually billed.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def usage_report() -> dict[str, Any]:
    return usage.summarize(_store())
