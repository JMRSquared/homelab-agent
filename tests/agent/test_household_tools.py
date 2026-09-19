import pytest

from agent.tools import base, household


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
