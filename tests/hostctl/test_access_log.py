"""hostctl.access must reach the journal under uvicorn.

uvicorn configures only its own loggers, so these tests never call
caplog.at_level on hostctl.access: doing so would set the level the fix is
meant to set and hide the 2026-10-11 bug, where every request line was dropped
at the inherited WARNING level.
"""

import importlib
import logging

import pytest
from fastapi.testclient import TestClient

import hostctl.app
from hostctl import pve
from hostctl.app import app

TOKEN = "grd007-known-bearer-value"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", TOKEN)


def _access_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "hostctl.access"]


def test_GRD_006_authenticated_request_is_logged_at_info_by_its_own_handler(
    monkeypatch, caplog
):
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    r = TestClient(app).post(
        "/guest/105/action",
        json={"action": "reboot"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )

    assert r.status_code == 200
    records = _access_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "POST" in message
    assert "/guest/105/action" in message
    assert '{"action":"reboot"}' in message
    assert "status=200" in message

    importlib.reload(hostctl.app)
    hostctl.app.configure_access_log()
    access = logging.getLogger("hostctl.access")
    assert access.getEffectiveLevel() == logging.INFO
    assert len(access.handlers) == 1
    assert isinstance(access.handlers[0], logging.StreamHandler)


def test_GRD_007_access_log_never_contains_the_bearer_token(monkeypatch, caplog):
    monkeypatch.setattr(pve, "_raw_guests", lambda: [])
    client = TestClient(app)

    client.get("/guests", headers={"Authorization": f"Bearer {TOKEN}"})
    client.get("/guests", headers={"Authorization": "Bearer wrong"})
    client.post("/jobs", json={"target": "nope"}, headers={"Authorization": f"Bearer {TOKEN}"})

    records = _access_records(caplog)
    assert len(records) == 3
    journal = logging.getLogger("hostctl.access").handlers[0]
    for record in records:
        line = journal.format(record)
        assert TOKEN not in line
        assert "Bearer" not in line
