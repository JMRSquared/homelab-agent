import pytest
from fastapi.testclient import TestClient

from hostctl import pve
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def test_action_on_vm_200_is_403():
    r = TestClient(app).post("/guest/200/action", json={"action": "stop"}, headers=AUTH)
    assert r.status_code == 403


def test_unknown_action_is_422():
    r = TestClient(app).post("/guest/101/action", json={"action": "destroy"}, headers=AUTH)
    assert r.status_code == 422


def test_reboot_invokes_pct(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post("/guest/101/action", json={"action": "reboot"}, headers=AUTH)
    assert r.status_code == 200
    assert seen == [["pct", "reboot", "101"]]


def test_exec_rejects_command_outside_allowlist(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post(
        "/guest/101/exec", json={"argv": ["rm", "-rf", "/"]}, headers=AUTH
    )
    assert r.status_code == 403
