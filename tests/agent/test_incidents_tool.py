import pytest

from agent.tools import base, incidents  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DB", str(tmp_path / "agent.db"))


def test_incident_record_then_find_round_trip():
    out = base.dispatch(
        "incident_record",
        {
            "component": "hostctl",
            "symptom": "guest_exec to 104 returns a 500",
            "cause": "hostctl restarted mid-request",
            "fix": "retried after 5s",
        },
    )
    assert out["ok"] is True
    assert out["result"]["id"] >= 1

    out = base.dispatch(
        "incident_find",
        {"component": "hostctl", "symptom": "hostctl 500 on guest 104 exec"},
    )
    assert out["ok"] is True
    assert len(out["result"]["matches"]) == 1
    assert out["result"]["matches"][0]["fix"] == "retried after 5s"


def test_incident_find_with_no_matches_is_still_ok():
    out = base.dispatch("incident_find", {"component": "zfs", "symptom": "pool degraded"})
    assert out["ok"] is True
    assert out["result"]["matches"] == []


def test_incident_record_rejects_blank_fields():
    out = base.dispatch(
        "incident_record",
        {"component": "", "symptom": "x", "cause": "x", "fix": "x"},
    )
    assert out["ok"] is False
