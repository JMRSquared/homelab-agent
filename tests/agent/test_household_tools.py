from types import SimpleNamespace
from typing import Any

import pytest

from agent.tools import base, household


class FakeEvent:
    """Stands in for a caldav Event: what calendar_list reads off search() results,
    and what calendar_add gets back from save_event()."""

    def __init__(self, summary: str, start: str, event_id: str = "evt-1") -> None:
        self.id = event_id
        self.vobject_instance = SimpleNamespace(
            vevent=SimpleNamespace(
                summary=SimpleNamespace(value=summary),
                dtstart=SimpleNamespace(value=start),
            )
        )


class FakeCalendar:
    def __init__(self, events: list[FakeEvent] | None = None) -> None:
        self._events = events or []
        self.save_event_calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> list[FakeEvent]:
        return self._events

    def save_event(self, **kwargs: Any) -> FakeEvent:
        self.save_event_calls.append(kwargs)
        return FakeEvent(kwargs["summary"], str(kwargs["dtstart"]), event_id="evt-42")


class FakePrincipal:
    def __init__(self, calendar: FakeCalendar) -> None:
        self._calendar = calendar

    def calendars(self) -> list[FakeCalendar]:
        return [self._calendar]


class FakeDAVClient:
    def __init__(self, calendar: FakeCalendar) -> None:
        self._calendar = calendar

    def principal(self) -> FakePrincipal:
        return FakePrincipal(self._calendar)


def _patch_calendar(monkeypatch: pytest.MonkeyPatch, calendar: FakeCalendar) -> None:
    monkeypatch.setattr(household, "DAVClient", lambda **kwargs: FakeDAVClient(calendar))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_LISTS", str(tmp_path))
    monkeypatch.setenv("CALDAV_URL", "http://10.0.0.165:5232/family/home/")
    monkeypatch.setenv("CALDAV_USER", "family")
    monkeypatch.setenv("CALDAV_PASSWORD", "pw")


def test_notes_append_creates_and_appends():
    base.dispatch("notes_append", {"list_name": "shopping", "item": "milk"})
    out = base.dispatch("notes_append", {"list_name": "shopping", "item": "bread"})
    assert out["ok"] is True
    assert out["result"]["items"] == ["milk", "bread"]


def test_notes_append_rejects_path_traversal():
    out = base.dispatch("notes_append", {"list_name": "../../etc/passwd", "item": "x"})
    assert out["ok"] is False


def test_notes_append_rejects_path_traversal_direct_call():
    # dispatch validates against the schema, but tick.py calls tools directly,
    # bypassing schema validation entirely. The function body must reject on its own.
    with pytest.raises(ValueError):
        household.notes_append(list_name="../../etc/passwd", item="x")


def test_calendar_add_rejects_non_iso_dates():
    out = base.dispatch(
        "calendar_add",
        {"title": "dentist", "start": "next tuesday", "end": "later", "who": "mum"},
    )
    assert out["ok"] is False


def test_calendar_add_rejects_naive_datetimes():
    # No UTC offset: ambiguous which timezone the family means. Reject rather than guess.
    out = base.dispatch(
        "calendar_add",
        {
            "title": "dentist",
            "start": "2026-09-22T14:00:00",
            "end": "2026-09-22T15:00:00",
            "who": "mum",
        },
    )
    assert out["ok"] is False


def test_household_tools_are_registered():
    assert {"calendar_list", "calendar_add", "notes_append"} <= base.REGISTRY.keys()


def test_notes_append_rejects_trailing_newline():
    # Python's `$` matches just before a single trailing newline, not only at the
    # true end of string — "shopping\n" must not sneak past either the schema
    # pattern or the body check into a filename.
    out = base.dispatch("notes_append", {"list_name": "shopping\n", "item": "milk"})
    assert out["ok"] is False


def test_notes_append_rejects_trailing_newline_direct_call():
    with pytest.raises(ValueError):
        household.notes_append(list_name="shopping\n", item="milk")


def test_calendar_list_extracts_summary_and_start(monkeypatch):
    calendar = FakeCalendar(events=[FakeEvent("dentist", "2026-09-22 14:00:00+02:00")])
    _patch_calendar(monkeypatch, calendar)

    out = base.dispatch("calendar_list", {"days": 7})

    assert out["ok"] is True
    assert out["result"]["events"] == [
        {"summary": "dentist", "start": "2026-09-22 14:00:00+02:00"}
    ]


def test_calendar_list_returns_empty_when_no_events(monkeypatch):
    _patch_calendar(monkeypatch, FakeCalendar(events=[]))

    out = base.dispatch("calendar_list", {"days": 7})

    assert out == {"ok": True, "result": {"events": []}}


def test_calendar_add_returns_added_uid_and_summary(monkeypatch):
    calendar = FakeCalendar()
    _patch_calendar(monkeypatch, calendar)

    out = base.dispatch(
        "calendar_add",
        {
            "title": "dentist",
            "start": "2026-09-22T14:00:00+02:00",
            "end": "2026-09-22T15:00:00+02:00",
            "who": "mum",
        },
    )

    assert out["ok"] is True
    assert out["result"] == {"added": True, "uid": "evt-42", "summary": "dentist (mum)"}
    # The attribute chain is genuinely exercised: the summary that reached the
    # (fake) caldav call is the one the tool claims it saved.
    assert calendar.save_event_calls[0]["summary"] == "dentist (mum)"
