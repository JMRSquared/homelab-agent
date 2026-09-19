import datetime as dt
import os
import re
import uuid
from pathlib import Path
from typing import Any, cast

from caldav.calendarobjectresource import Event
from caldav.collection import Calendar
from caldav.davclient import DAVClient

from agent.tools.base import tool

# A shared list's name becomes a filename under AGENT_LISTS: no "/", no leading
# "-", no ".." traversal tricks, lowercase-first so it reads as a plain word.
SAFE_NAME_PATTERN = r"^[a-z0-9][a-z0-9 _-]{0,40}$"
SAFE_NAME = re.compile(SAFE_NAME_PATTERN)


def _lists_dir() -> Path:
    path = Path(os.environ.get("AGENT_LISTS", "/tank/dev/agent/lists"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _calendar() -> Calendar:
    client = DAVClient(
        url=os.environ["CALDAV_URL"],
        username=os.environ["CALDAV_USER"],
        password=os.environ["CALDAV_PASSWORD"],
    )
    principal = client.principal()  # type: ignore[no-untyped-call]
    calendars = cast(list[Calendar], principal.calendars())
    if not calendars:
        raise RuntimeError("Radicale has no calendars configured for this principal")
    return calendars[0]


def _iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(
            f"{value!r} has no UTC offset; use e.g. 2026-09-22T14:00:00+02:00"
        )
    return parsed


@tool(
    "calendar_list",
    "List family calendar events starting from now through the next N days. Use this "
    "to check what's already on the calendar, e.g. 'what's happening this weekend' or "
    "'is anything on for Tuesday'.",
    {
        "type": "object",
        "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 90}},
        "required": ["days"],
        "additionalProperties": False,
    },
)
def calendar_list(days: int) -> dict[str, Any]:
    start = dt.datetime.now(dt.UTC)
    events = cast(
        list[Event],
        _calendar().search(
            start=start, end=start + dt.timedelta(days=days), event=True, expand=True
        ),
    )
    return {
        "events": [
            {
                "summary": str(e.vobject_instance.vevent.summary.value),
                "start": str(e.vobject_instance.vevent.dtstart.value),
            }
            for e in events
        ]
    }


@tool(
    "calendar_add",
    "Add a new event to the shared family calendar, such as an appointment, pickup, or "
    "reminder someone asks to be put on the calendar (e.g. 'add dentist Tuesday at 2pm "
    "for mum'). Start and end must be ISO 8601 timestamps with a UTC offset, for example "
    "2026-09-22T14:00:00+02:00.",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "start": {"type": "string", "minLength": 1, "maxLength": 40},
            "end": {"type": "string", "minLength": 1, "maxLength": 40},
            "who": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "required": ["title", "start", "end", "who"],
        "additionalProperties": False,
    },
)
def calendar_add(title: str, start: str, end: str, who: str) -> dict[str, Any]:
    begins, ends = _iso(start), _iso(end)
    summary = f"{title} ({who})"
    event = cast(
        Event,
        _calendar().save_event(
            dtstart=begins, dtend=ends, summary=summary, uid=str(uuid.uuid4())
        ),
    )
    return {"added": True, "uid": str(event.id), "summary": summary}


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
    if not SAFE_NAME.match(list_name):
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
