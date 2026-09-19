import pytest

from agent.tools import base, household


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_LISTS", str(tmp_path))


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


def test_household_tools_are_registered():
    assert {"notes_append"} <= base.REGISTRY.keys()


def test_notes_append_rejects_trailing_newline():
    # Python's `$` matches just before a single trailing newline, not only at the
    # true end of string — "shopping\n" must not sneak past either the schema
    # pattern or the body check into a filename.
    out = base.dispatch("notes_append", {"list_name": "shopping\n", "item": "milk"})
    assert out["ok"] is False


def test_notes_append_rejects_trailing_newline_direct_call():
    with pytest.raises(ValueError):
        household.notes_append(list_name="shopping\n", item="milk")
