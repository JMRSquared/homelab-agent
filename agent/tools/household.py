import os
import re
from pathlib import Path
from typing import Any

from agent.tools.base import tool

# A shared list's name becomes a filename under AGENT_LISTS: no "/", no leading
# "-", no ".." traversal tricks, lowercase-first so it reads as a plain word. The
# `(?!.*\n)` lookahead is needed because Python's `$` (used by both this schema
# pattern and the body-level regex below) matches just before a single trailing
# newline, not only at the true end of string — without it "shopping\n" would pass.
SAFE_NAME_PATTERN = r"^(?!.*\n)[a-z0-9][a-z0-9 _-]{0,40}$"
SAFE_NAME = re.compile(SAFE_NAME_PATTERN)


def _lists_dir() -> Path:
    path = Path(os.environ.get("AGENT_LISTS", "/tank/dev/agent/lists"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@tool(
    "notes_append",
    "Append one item to a shared household list, such as the shopping list or a chores "
    "list (e.g. 'put milk on the shopping list', 'add mow the lawn to chores'). Creates "
    "the list if it doesn't exist yet.",
    {
        "type": "object",
        "properties": {
            "list_name": {"type": "string", "pattern": SAFE_NAME_PATTERN},
            "item": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        "required": ["list_name", "item"],
        "additionalProperties": False,
    },
)
def notes_append(list_name: str, item: str) -> dict[str, Any]:
    # fullmatch is the authoritative check: it requires the match to span the
    # whole string, so it isn't fooled by "$"'s trailing-newline exception the
    # way a plain .match/.search against this pattern would be.
    if not SAFE_NAME.fullmatch(list_name):
        raise ValueError(f"invalid list name: {list_name!r}")
    path = _lists_dir() / f"{list_name}.md"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {item}\n")
    items = [
        line[2:].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("- ")
    ]
    return {"list": list_name, "items": items}
