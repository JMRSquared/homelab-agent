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
