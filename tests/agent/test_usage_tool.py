import pytest

from agent.store import Store
from agent.tools import base, usage  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "agent.db"))
    return tmp_path


def test_usage_report_reflects_recorded_usage(monkeypatch, tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    store.record_usage(
        context="daemon", model="MiniMax-M3", prompt_tokens=10, completion_tokens=5,
        total_tokens=15,
    )
    out = base.dispatch("usage_report", {})
    assert out["ok"] is True
    assert out["result"]["today"]["total"]["total_tokens"] == 15
    assert "not a bill" in out["result"]["note"]


def test_usage_report_on_empty_store_is_zero(tmp_path):
    out = base.dispatch("usage_report", {})
    assert out["ok"] is True
    assert out["result"]["today"]["total"]["requests"] == 0
