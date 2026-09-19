import json

import pytest

from hostctl import pve

NODES_JSON = json.dumps([{"node": "tech"}])
LXC_JSON = json.dumps(
    [
        {"vmid": 101, "name": "docker", "status": "running"},
        {"vmid": 103, "name": "mail", "status": "running"},
    ]
)
QEMU_JSON = json.dumps([{"vmid": 200, "name": "mt5", "status": "running"}])


def test_raw_guests_invokes_pvesh_with_resolved_node(monkeypatch):
    """Regression test for the live-host defect: `pct list --output-format
    json` / `qm list --output-format json` do not exist on this Proxmox
    version. This test mocks only `_run` (the actual subprocess boundary),
    not `_raw_guests` itself, so the exact command shape is asserted rather
    than assumed."""
    seen: list[list[str]] = []

    def _fake_run(argv: list[str]) -> str:
        seen.append(argv)
        if argv == ["pvesh", "get", "/nodes", "--output-format", "json"]:
            return NODES_JSON
        if argv == ["pvesh", "get", "/nodes/tech/lxc", "--output-format", "json"]:
            return LXC_JSON
        if argv == ["pvesh", "get", "/nodes/tech/qemu", "--output-format", "json"]:
            return QEMU_JSON
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(pve, "_run", _fake_run)

    guests = pve._raw_guests()

    assert seen == [
        ["pvesh", "get", "/nodes", "--output-format", "json"],
        ["pvesh", "get", "/nodes/tech/lxc", "--output-format", "json"],
        ["pvesh", "get", "/nodes/tech/qemu", "--output-format", "json"],
    ]
    assert [g["id"] for g in guests] == [101, 103, 200]
    assert [g["kind"] for g in guests] == ["lxc", "lxc", "qemu"]


def test_node_name_raises_when_not_exactly_one_node(monkeypatch):
    monkeypatch.setattr(
        pve, "_run", lambda argv: json.dumps([{"node": "a"}, {"node": "b"}])
    )
    with pytest.raises(RuntimeError):
        pve._node_name()


def test_raw_guests_skips_malformed_rows(monkeypatch):
    """Regression test: one malformed row from pvesh (missing vmid or
    status) must not turn the whole guest listing into an unhandled 500 -
    it should be skipped, and every well-formed row still returned."""
    malformed_lxc = json.dumps(
        [
            {"vmid": 101, "name": "docker", "status": "running"},
            {"name": "no-vmid", "status": "running"},
            {"vmid": 105, "name": "no-status"},
        ]
    )

    def _fake_run(argv: list[str]) -> str:
        if argv == ["pvesh", "get", "/nodes", "--output-format", "json"]:
            return NODES_JSON
        if argv == ["pvesh", "get", "/nodes/tech/lxc", "--output-format", "json"]:
            return malformed_lxc
        if argv == ["pvesh", "get", "/nodes/tech/qemu", "--output-format", "json"]:
            return "[]"
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(pve, "_run", _fake_run)

    guests = pve._raw_guests()
    assert [g["id"] for g in guests] == [101]


def test_guest_exec_runs_with_the_longer_exec_timeout(monkeypatch):
    import subprocess

    seen: dict[str, object] = {}

    def _fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    monkeypatch.setattr(pve, "_run_full", _fake_run_full)
    pve.guest_exec(101, ["docker", "compose", "pull"])
    assert seen["timeout"] == pve.EXEC_TIMEOUT
    assert seen["timeout"] > pve.DEFAULT_TIMEOUT


def test_guest_exec_dispatches_to_pct_for_lxc(monkeypatch):
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "pct"
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="lxc output", stderr=""
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    out = pve.guest_exec(101, ["uptime"])
    assert out == {
        "guest": 101,
        "argv": ["uptime"],
        "stdout": "lxc output",
        "stderr": "",
        "exitcode": 0,
    }


def test_guest_exec_has_no_command_allowlist_for_lxc(monkeypatch):
    """Regression test: ALLOWED_EXEC was removed entirely - no restriction
    the owner didn't ask for."""
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    out = pve.guest_exec(101, ["rm", "-rf", "/tmp/scratch"])
    assert out["exitcode"] == 0


def test_guest_exec_rejects_empty_argv(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    with pytest.raises(PermissionError):
        pve.guest_exec(101, [])


def test_guest_exec_dispatches_to_qm_guest_exec_for_qemu(monkeypatch):
    """Regression test for VM 200 parity: guest_exec must reach a QEMU
    guest (VM 200, a Windows VM) via `qm guest exec`, not the LXC-only
    `pct exec` path, and ALLOWED_EXEC (a Linux command list) must not gate
    it - the owner asked for full administrative control over VM 200."""
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    seen: dict[str, object] = {}

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["timeout"] = timeout
        assert argv[:3] == ["qm", "guest", "exec"]
        assert "--timeout" in argv
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"out-data": "C:\\\\Windows", "err-data": "", "exitcode": 0}',
            stderr="",
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    # "tasklist" isn't in ALLOWED_EXEC (a Linux-only list) and must not be
    # rejected on that basis for a QEMU target.
    out = pve.guest_exec(200, ["tasklist"])
    assert out == {
        "guest": 200,
        "argv": ["tasklist"],
        "stdout": "C:\\Windows",
        "stderr": "",
        "exitcode": 0,
    }
    assert seen["argv"][3] == "200"


def test_guest_exec_qemu_nonzero_exitcode_is_not_an_error(monkeypatch):
    """A command that reached the guest and failed there (a real nonzero
    exitcode inside a successful envelope) is not the same thing as the
    guest agent being unavailable - it's a normal result the caller can
    inspect, not an exception."""
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"out-data": "", "err-data": "not found", "exitcode": 1}',
            stderr="",
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    out = pve.guest_exec(200, ["nonexistent-command"])
    assert out["exitcode"] == 1
    assert out["stderr"] == "not found"


def test_guest_exec_qemu_reports_typed_error_when_guest_agent_is_down(monkeypatch):
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            255, argv, output="", stderr="QEMU guest agent is not running"
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    with pytest.raises(pve.GuestAgentUnavailableError, match="guest agent"):
        pve.guest_exec(200, ["tasklist"])


def test_guest_exec_qemu_reports_typed_error_on_timeout(monkeypatch):
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    with pytest.raises(pve.GuestAgentUnavailableError):
        pve.guest_exec(200, ["tasklist"])


def test_guest_exec_qemu_other_failures_propagate(monkeypatch):
    """A CalledProcessError unrelated to the guest agent (e.g. a genuine
    qm-level error) must not be swallowed into the typed
    GuestAgentUnavailableError - only the guest-agent-shaped failure is."""
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv, output="", stderr="unknown vmid")

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    with pytest.raises(subprocess.CalledProcessError):
        pve.guest_exec(200, ["tasklist"])


def test_guest_exec_lxc_timeout_raises_typed_error(monkeypatch):
    """Regression test: a hung LXC exec used to propagate a bare
    subprocess.TimeoutExpired with nothing above hostctl catching it."""
    import subprocess

    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    def fake_run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    with pytest.raises(pve.GuestCommandTimeoutError):
        pve.guest_exec(101, ["sleep", "999999"])


def test_guest_shell_wraps_command_in_sh_c_for_lxc(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")

    seen: dict[str, object] = {}

    def fake_run_full(argv: list[str], *, timeout: int) -> object:
        seen["argv"] = argv
        import subprocess as sp

        return sp.CompletedProcess(args=argv, returncode=0, stdout="mail body\n", stderr="")

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    out = pve.guest_shell(101, "cat /var/mail/someone")
    assert seen["argv"] == ["pct", "exec", "101", "--", "sh", "-c", "cat /var/mail/someone"]
    assert out["stdout"] == "mail body\n"


def test_guest_shell_wraps_command_in_cmd_c_for_qemu(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "qemu")

    seen: dict[str, object] = {}

    def fake_run_full(argv: list[str], *, timeout: int) -> object:
        seen["argv"] = argv
        import subprocess as sp

        return sp.CompletedProcess(
            args=argv,
            returncode=0,
            stdout='{"out-data": "ok", "err-data": "", "exitcode": 0}',
            stderr="",
        )

    monkeypatch.setattr(pve, "_run_full", fake_run_full)
    out = pve.guest_shell(200, "hostname")
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[-3:] == ["cmd.exe", "/c", "hostname"]
    assert out["stdout"] == "ok"


def test_guest_shell_rejects_blank_command(monkeypatch):
    monkeypatch.setattr(pve, "_kind_of", lambda gid: "lxc")
    with pytest.raises(PermissionError):
        pve.guest_shell(101, "   ")
