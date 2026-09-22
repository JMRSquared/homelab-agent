import pytest

from agent.tools import base, memory


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BRAIN", str(tmp_path / "brain.md"))


def test_brain_write_then_brain_read_round_trip():
    out = base.dispatch("brain_write", {"topic": "jellyfin", "content": "restarts weekly"})
    assert out["ok"] is True
    assert out["result"] == {"topic": "jellyfin", "saved": True}

    out = base.dispatch("brain_read", {"topic": "jellyfin"})
    assert out["ok"] is True
    assert out["result"] == {"topic": "jellyfin", "content": "restarts weekly"}


def test_brain_read_unknown_topic_is_empty_not_an_error():
    out = base.dispatch("brain_read", {"topic": "nothing"})
    assert out["ok"] is True
    assert out["result"]["content"] == ""


def test_brain_write_rejects_topic_that_would_corrupt_the_file():
    out = base.dispatch("brain_write", {"topic": "## injected", "content": "x"})
    assert out["ok"] is False


def test_brain_write_rejects_bad_topic_direct_call():
    # dispatch validates against the schema, but tick.py calls tools directly,
    # bypassing schema validation entirely. The function body must reject on its own.
    with pytest.raises(ValueError):
        memory.brain_write(topic="not\nsafe", content="x")


def test_brain_list_returns_every_topic():
    base.dispatch("brain_write", {"topic": "jellyfin", "content": "note one"})
    base.dispatch("brain_write", {"topic": "zfs", "content": "note two"})
    out = base.dispatch("brain_list", {})
    assert out["ok"] is True
    assert out["result"]["topics"] == {"jellyfin": "note one", "zfs": "note two"}


def test_brain_consolidate_merges_and_backs_up():
    base.dispatch("brain_write", {"topic": "jellyfin", "content": "original"})
    out = base.dispatch(
        "brain_consolidate", {"updates": {"jellyfin": "merged"}, "drop": [], "contradictions": []}
    )
    assert out["ok"] is True
    assert out["result"]["updated_topics"] == ["jellyfin"]
    assert out["result"]["backup_path"]

    out = base.dispatch("brain_read", {"topic": "jellyfin"})
    assert out["result"]["content"] == "merged"


def test_brain_consolidate_flags_contradiction_without_resolving():
    base.dispatch("brain_write", {"topic": "jellyfin", "content": "restarts weekly"})
    out = base.dispatch(
        "brain_consolidate", {"contradictions": ["jellyfin: weekly vs never restarts"]}
    )
    assert out["ok"] is True
    assert out["result"]["contradictions_recorded"] == 1
    # The original topic is left alone.
    assert base.dispatch("brain_read", {"topic": "jellyfin"})["result"]["content"] == (
        "restarts weekly"
    )
    flagged = base.dispatch("brain_read", {"topic": "Contradictions"})["result"]["content"]
    assert "weekly vs never restarts" in flagged
