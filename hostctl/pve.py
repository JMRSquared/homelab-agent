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
# and the system prompts in agent/slack_app.py and agent/tick.py for the
# operational guardrails that replace the old hard block (verify after any
# stop/reboot, never place or modify a trade, no trading advice). Those are
# prompt-level, not enforced here, by design: the request was for oversight
# and administration, not for another invisible wall.
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


# Allowlisted for LXC targets only, where `pct exec` runs a real Linux
# shell command directly - this is the boundary that keeps an LXC exec call
# to read/service-management commands. It has no meaning for a Windows
# QEMU guest (see the QEMU exec path below, which does not use it): the
# owner asked for full administrative control over VM 200 specifically,
# reversing the project's previous invisible-VM constraint, and inventing a
# parallel Windows allowlist they didn't ask for would just be the same
# restriction under a new name. The operational guardrails for VM 200 live
# in the system prompts instead (agent/slack_app.py, agent/tick.py): verify
# after any stop/reboot given what a forced MetaTrader restart costs, never
# place or modify a trade, no trading advice.
ALLOWED_EXEC: frozenset[str] = frozenset(
    {"systemctl", "docker", "journalctl", "df", "free", "uptime", "ss", "curl"}
)

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


def _lxc_guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    result = _run_full(["pct", "exec", str(guest_id), "--", *argv], timeout=EXEC_TIMEOUT)
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
    if not argv:
        raise PermissionError("no command given")
    kind = _kind_of(guest_id)
    if kind == "lxc":
        if argv[0] not in ALLOWED_EXEC:
            raise PermissionError(f"command not allowed: {argv[:1]}")
        return _lxc_guest_exec(guest_id, argv)
    return _qemu_guest_exec(guest_id, argv)
