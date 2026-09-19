import subprocess

import pytest
from fastapi.testclient import TestClient

from hostctl import pve, zfs
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def test_action_on_vm_200_reaches_qm_not_blocked(monkeypatch):
    """Regression test for VM 200 parity: the owner gave the agent full
    administrative control over the trading VM, reversing the old blanket
    403. Guest 200 goes through the same guest_action path as any other
    QEMU guest now."""
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")
    r = TestClient(app).post("/guest/200/action", json={"action": "stop"}, headers=AUTH)
    assert r.status_code == 200
    assert seen == [["qm", "stop", "200"]]


def test_unknown_action_is_422():
    r = TestClient(app).post("/guest/101/action", json={"action": "destroy"}, headers=AUTH)
    assert r.status_code == 422


def test_reboot_invokes_pct(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post("/guest/105/action", json={"action": "reboot"}, headers=AUTH)
    assert r.status_code == 200
    assert seen == [["pct", "reboot", "105"]]


def test_reboot_on_self_protected_guest_is_403(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def _boom(argv: list[str]) -> str:
        raise AssertionError("subprocess must not run for a self-protected guest")

    monkeypatch.setattr(pve, "_run", _boom)
    r = TestClient(app).post("/guest/104/action", json={"action": "stop"}, headers=AUTH)
    assert r.status_code == 403


def test_start_on_self_protected_guest_is_allowed(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(pve, "_run", lambda argv: seen.append(argv) or "")
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    r = TestClient(app).post("/guest/104/action", json={"action": "start"}, headers=AUTH)
    assert r.status_code == 200
    assert seen == [["pct", "start", "104"]]


def test_exec_has_no_command_allowlist(monkeypatch):
    """Regression test: ALLOWED_EXEC was removed - the owner asked for full
    administrative access to every guest, with no restriction they didn't
    ask for. Any argv[0] reaches the guest now."""
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["pct", "exec", "101", "--", "rm", "-rf", "/tmp/scratch"]
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post(
        "/guest/101/exec", json={"argv": ["rm", "-rf", "/tmp/scratch"]}, headers=AUTH
    )
    assert r.status_code == 200


def test_shell_runs_a_free_form_command_on_an_lxc_guest(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv == ["pct", "exec", "101", "--", "sh", "-c", "grep -r trade /var/mail"]
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="hit\n", stderr="")

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post(
        "/guest/101/shell", json={"command": "grep -r trade /var/mail"}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["stdout"] == "hit\n"


def test_shell_runs_a_free_form_command_on_the_qemu_guest(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["qm", "guest", "exec"]
        assert argv[-3:] == ["cmd.exe", "/c", "hostname"]
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"out-data": "MT5\\r\\n", "err-data": "", "exitcode": 0}',
            stderr="",
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post("/guest/200/shell", json={"command": "hostname"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["stdout"] == "MT5\r\n"


def test_shell_command_timeout_returns_504(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=pve.EXEC_TIMEOUT)

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post(
        "/guest/101/shell", json={"command": "sleep 999999"}, headers=AUTH
    )
    assert r.status_code == 504


def test_exec_failure_returns_422_with_stderr_detail(monkeypatch):
    def _boom(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1, argv, output="", stderr="no configuration file provided: not found"
        )

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    monkeypatch.setattr(pve, "_run_full", _boom)
    r = TestClient(app).post(
        "/guest/101/exec", json={"argv": ["docker", "compose", "pull"]}, headers=AUTH
    )
    assert r.status_code == 422
    assert "not found" in r.json()["detail"]


def test_exec_on_vm_200_reaches_the_qemu_guest_agent_path(monkeypatch):
    """Regression test for VM 200 parity: guest 200 exec is dispatched to
    `qm guest exec` (it's a QEMU VM) the same way any other QEMU guest
    would be, not rejected outright the way the old blanket 403 did."""
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["qm", "guest", "exec"]
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"out-data": "ok", "err-data": "", "exitcode": 0}',
            stderr="",
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post("/guest/200/exec", json={"argv": ["tasklist"]}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["stdout"] == "ok"


def test_exec_reports_503_when_qemu_guest_agent_is_unavailable(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            255, argv, output="", stderr="QEMU guest agent is not running"
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    r = TestClient(app).post("/guest/200/exec", json={"argv": ["tasklist"]}, headers=AUTH)
    assert r.status_code == 503
    assert "guest agent" in r.json()["detail"]


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
