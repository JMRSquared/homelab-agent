import pytest
from fastapi.testclient import TestClient

from hostctl import pve, zfs
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


def test_exec_on_vm_200_is_403(monkeypatch):
    def _boom(argv: list[str]) -> str:
        raise AssertionError("subprocess must not run for a blocked guest")

    monkeypatch.setattr(pve, "_run", _boom)
    r = TestClient(app).post(
        "/guest/200/exec", json={"argv": ["systemctl", "status", "foo"]}, headers=AUTH
    )
    assert r.status_code == 403


def test_guest_action_rejects_disallowed_action(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    with pytest.raises(PermissionError):
        pve.guest_action(101, "destroy")  # type: ignore[arg-type]


def test_zfs_snapshot_route_rejects_dataset_outside_tank():
    r = TestClient(app).post(
        "/zfs/snapshot", json={"dataset": "rpool/other", "label": "x"}, headers=AUTH
    )
    assert r.status_code == 400


def test_zfs_snapshot_route_rate_limits(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda argv: "")
    client = TestClient(app)
    first = client.post(
        "/zfs/snapshot", json={"dataset": "tank/immich", "label": "x"}, headers=AUTH
    )
    assert first.status_code == 200

    def fake_run(argv: list[str]) -> str:
        is_snapshot_read = argv[-2:] == ["-r", "tank/immich"] or (
            argv[:2] == ["zfs", "list"] and "tank/immich" in argv
        )
        if is_snapshot_read:
            import datetime as dt

            now = dt.datetime.now(dt.UTC)
            return f"tank/immich@x-20260101T000000Z\t{int(now.timestamp())}\n"
        return ""

    monkeypatch.setattr(zfs, "_run", fake_run)
    second = client.post(
        "/zfs/snapshot", json={"dataset": "tank/immich", "label": "x"}, headers=AUTH
    )
    assert second.status_code == 429
