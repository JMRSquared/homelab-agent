import json
import subprocess
from typing import Literal, TypedDict

# The agent itself is LXC 104; the services and exec path it would use to
# recover (docker, the compose stacks) live in LXC 101. Stopping or
# rebooting either strands the agent - it cannot restart itself, because
# `pct start 104` is only reachable *through* itself. `start` stays allowed
# for both: bringing either one up can never strand anything.
# agent/tools/infra.py enforces the same restriction independently, since
# `agent/tick.py` calls tool functions directly and bypasses hostctl's HTTP
# boundary entirely for some paths.
#
# There is no equivalent set for VM 200 (mt5) any more. The owner decided
# the agent oversees the whole homelab, including the trading VM, with full
# administrative control and no approval prompts - see docs/homelab-agent-spec.md
# and the system prompt in agent/prompts.py for the operational knowledge
# (what a stop/reboot costs) that replaces the old hard block. That
# knowledge is prompt-level, not enforced here, by design: the request was
# for oversight and administration, not for another invisible wall. Note
# that `guest_shell` below reaches every guest with no command allowlist at
# all, including 101/104 - SELF_PROTECTED_GUEST_IDS only gates
# guest_action's stop/reboot, not exec. A command run via guest_shell on
# 101 or 104 could still strand the agent; that door is open by the same
# "no restrictions they did not ask for" reasoning as everything else here.
SELF_PROTECTED_GUEST_IDS: frozenset[int] = frozenset({101, 104})

DEFAULT_TIMEOUT = 30
# `docker compose pull` for a Jellyfin/Immich-sized image can take minutes;
# the fixed 30s timeout every other call uses would abort a pull that was
# still legitimately running on the host. agent/clients.py's EXEC_TIMEOUT
# must stay >= this value. Also used as the `qm guest exec --timeout` value
# and the surrounding subprocess timeout for the QEMU exec path below.
EXEC_TIMEOUT = 300


class Guest(TypedDict):
    id: int
    name: str
    kind: Literal["lxc", "qemu"]
    status: str


class GuestAgentUnavailableError(RuntimeError):
    """The QEMU guest agent inside a VM didn't answer an exec request.

    The most likely real-world condition for `qm guest exec` to fail: the
    guest agent service isn't running (stopped, not installed, or the VM is
    still booting), so the command never actually reached the guest. Kept
    distinct from a command that reached the guest and failed there, which
    surfaces as a normal non-zero `exitcode` in a successful envelope
    instead of this exception.
    """


def _run(argv: list[str], *, timeout: int = DEFAULT_TIMEOUT) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=timeout).stdout


def _run_full(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Like `_run`, but returns the whole CompletedProcess (stdout, stderr,
    return code) rather than just stdout - the exec paths need to report
    all three in the same shape regardless of which one (`pct`/`qm`)
    actually ran the command."""
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=timeout)


def _node_name() -> str:
    """Resolve the Proxmox node name by asking pvesh, rather than assuming
    it matches the machine hostname. Raises rather than guessing when the
    cluster doesn't have exactly one node, so a caller never silently
    queries a node that doesn't exist."""
    rows = json.loads(_run(["pvesh", "get", "/nodes", "--output-format", "json"]))
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one Proxmox node, found {len(rows)}")
    return str(rows[0]["node"])


def _raw_guests() -> list[Guest]:
    node = _node_name()
    out: list[Guest] = []
    for kind, path in (("lxc", f"/nodes/{node}/lxc"), ("qemu", f"/nodes/{node}/qemu")):
        rows = json.loads(_run(["pvesh", "get", path, "--output-format", "json"]))
        for row in rows:
            # `.get`, not `row["vmid"]`/`row["status"]`: one malformed row
            # from pvesh must not turn every guest listing into a 500. Skip
            # it and keep the rest.
            vmid = row.get("vmid")
            status = row.get("status")
            if vmid is None or status is None:
                continue
            out.append(
                Guest(
                    id=int(vmid),
                    name=str(row.get("name") or row.get("hostname") or ""),
                    kind=kind,  # type: ignore[typeddict-item]
                    status=str(status),
                )
            )
    return out


def list_guests() -> list[Guest]:
    """Every guest on the node, unfiltered. VM 200 (mt5) is a guest like any
    other now - see the note on SELF_PROTECTED_GUEST_IDS above."""
    return _raw_guests()


ALLOWED_ACTIONS: frozenset[str] = frozenset({"start", "stop", "reboot"})
PROTECTED_ACTIONS: frozenset[str] = frozenset({"stop", "reboot"})


def _kind_of(guest_id: int) -> str:
    for g in _raw_guests():
        if g["id"] == guest_id:
            return g["kind"]
    raise ValueError(f"unknown guest {guest_id}")


def guest_action(guest_id: int, action: Literal["start", "stop", "reboot"]) -> dict[str, str]:
    if action not in ALLOWED_ACTIONS:
        raise PermissionError(f"action not allowed: {action}")
    if guest_id in SELF_PROTECTED_GUEST_IDS and action in PROTECTED_ACTIONS:
        raise PermissionError(
            f"guest {guest_id} hosts the agent itself or its recovery path "
            f"(docker/exec in LXC 101); {action} on it is blocked so the agent "
            "can't strand itself with no way to come back"
        )
    cmd = "pct" if _kind_of(guest_id) == "lxc" else "qm"
    _run([cmd, action, str(guest_id)])
    return {"guest": str(guest_id), "action": action, "result": "ok"}


class GuestCommandTimeoutError(RuntimeError):
    """A guest_exec/guest_shell command did not finish within EXEC_TIMEOUT.

    Distinct from GuestAgentUnavailableError: this is a command that started
    (or, for the QEMU path, one the guest agent never acknowledged - the two
    aren't distinguishable from the timeout alone) and simply ran too long -
    a hung `docker compose pull`, a `cmd.exe` command waiting on input that
    will never come, and so on. Without this, a hung command on the LXC
    path used to propagate a bare subprocess.TimeoutExpired, which was
    never caught anywhere above hostctl and would have stalled the caller
    rather than returning a typed error.
    """


def _lxc_guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    try:
        result = _run_full(["pct", "exec", str(guest_id), "--", *argv], timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise GuestCommandTimeoutError(
            f"command on guest {guest_id} did not finish within {EXEC_TIMEOUT}s"
        ) from exc
    return {
        "guest": guest_id,
        "argv": argv,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exitcode": result.returncode,
    }


def _qemu_guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    """Run `argv` inside a QEMU guest via `qm guest exec`.

    Needs the QEMU guest agent running inside the VM - unlike `pct exec`,
    which talks to the LXC's own namespace directly, this goes through an
    agent process inside the guest OS. `qm guest exec ... -- <argv>` blocks
    (up to `--timeout`) and prints a JSON envelope
    (`{"out-data": ..., "err-data": ..., "exitcode": ...}`) rather than raw
    stdout, so the result is unpacked and reshaped into the same
    {guest, argv, stdout, stderr, exitcode} shape `_lxc_guest_exec` returns
    - callers cannot tell which path actually ran.
    """
    try:
        raw = _run_full(
            ["qm", "guest", "exec", str(guest_id), "--timeout", str(EXEC_TIMEOUT), "--", *argv],
            timeout=EXEC_TIMEOUT + 10,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "guest agent" in stderr or "not running" in stderr or "no route to host" in stderr:
            raise GuestAgentUnavailableError(
                f"guest {guest_id}'s QEMU guest agent isn't responding - the VM may "
                "still be booting, the agent service may be stopped inside the "
                "guest, or it may not be installed"
            ) from exc
        raise
    except subprocess.TimeoutExpired as exc:
        raise GuestAgentUnavailableError(
            f"guest {guest_id}'s QEMU guest agent didn't respond within "
            f"{EXEC_TIMEOUT}s - it may be stopped inside the guest or the guest "
            "may be unresponsive"
        ) from exc

    try:
        envelope = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"qm guest exec returned non-JSON output for guest {guest_id}: {raw.stdout!r}"
        ) from exc

    return {
        "guest": guest_id,
        "argv": argv,
        "stdout": envelope.get("out-data", ""),
        "stderr": envelope.get("err-data", ""),
        "exitcode": envelope.get("exitcode"),
    }


def guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    """Run `argv` directly (no shell) on a guest. No command allowlist - the
    owner asked for full administrative access to every guest. Used by the
    narrow tools (docker_stacks/docker_action) that already know the exact
    argv they want; see `guest_shell` for a free-form command string."""
    if not argv:
        raise PermissionError("no command given")
    kind = _kind_of(guest_id)
    if kind == "lxc":
        return _lxc_guest_exec(guest_id, argv)
    return _qemu_guest_exec(guest_id, argv)


def guest_shell(guest_id: int, command: str) -> dict[str, object]:
    """Run a free-form shell one-liner on any guest, dispatched by kind:
    `sh -c <command>` for an LXC, `cmd.exe /c <command>` for a QEMU guest
    (VM 200's Windows). This is what backs the model-facing `guest_exec`
    tool in agent/tools/infra.py - a single string in the syntax native to
    that guest's OS, no allowlist, the general-purpose way to reach
    anything the narrow tools don't cover.
    """
    if not command.strip():
        raise PermissionError("no command given")
    kind = _kind_of(guest_id)
    if kind == "lxc":
        return _lxc_guest_exec(guest_id, ["sh", "-c", command])
    return _qemu_guest_exec(guest_id, ["cmd.exe", "/c", command])


def host_shell(command: str) -> dict[str, object]:
    """Run a free-form `sh -c <command>` directly on the Proxmox host itself
    - not a guest, no `pct`/`qm` involved. No command allowlist, same as
    `guest_shell` - the owner asked for full administrative access to the
    host, with no restriction they did not ask for.

    hostctl's own process runs on this host, so a command here can restart
    or kill hostctl, and can reach `/tank` (every guest's ZFS-backed data,
    and hostctl's own code/venv) directly without going through any guest.
    That is deliberate, not an oversight - see agent/tools/infra.py's
    `host_exec` docstring, which is what a model actually reads before
    calling this.

    Reuses `_run_full`/`GuestCommandTimeoutError` rather than a second
    timeout implementation - the host is just one more thing this module
    runs a command against, and the timeout/output shape a caller needs is
    identical to `guest_shell`'s.
    """
    if not command.strip():
        raise PermissionError("no command given")
    try:
        result = _run_full(["sh", "-c", command], timeout=EXEC_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise GuestCommandTimeoutError(
            f"host command did not finish within {EXEC_TIMEOUT}s"
        ) from exc
    return {
        "argv": ["sh", "-c", command],
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exitcode": result.returncode,
    }
