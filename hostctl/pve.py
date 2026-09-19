import json
import subprocess
from typing import Literal, TypedDict

BLOCKED_GUEST_IDS: frozenset[int] = frozenset({200})


class Guest(TypedDict):
    id: int
    name: str
    kind: Literal["lxc", "qemu"]
    status: str


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=30).stdout


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
            out.append(
                Guest(
                    id=int(row["vmid"]),
                    name=str(row.get("name") or row.get("hostname") or ""),
                    kind=kind,  # type: ignore[typeddict-item]
                    status=str(row["status"]),
                )
            )
    return out


def list_guests() -> list[Guest]:
    return [g for g in _raw_guests() if g["id"] not in BLOCKED_GUEST_IDS]


ALLOWED_EXEC: frozenset[str] = frozenset(
    {"systemctl", "docker", "journalctl", "df", "free", "uptime", "ss", "curl"}
)

ALLOWED_ACTIONS: frozenset[str] = frozenset({"start", "stop", "reboot"})


def _kind_of(guest_id: int) -> str:
    for g in _raw_guests():
        if g["id"] == guest_id:
            return g["kind"]
    raise ValueError(f"unknown guest {guest_id}")


def guest_action(guest_id: int, action: Literal["start", "stop", "reboot"]) -> dict[str, str]:
    if action not in ALLOWED_ACTIONS:
        raise PermissionError(f"action not allowed: {action}")
    cmd = "pct" if _kind_of(guest_id) == "lxc" else "qm"
    _run([cmd, action, str(guest_id)])
    return {"guest": str(guest_id), "action": action, "result": "ok"}


def guest_exec(guest_id: int, argv: list[str]) -> dict[str, object]:
    if not argv or argv[0] not in ALLOWED_EXEC:
        raise PermissionError(f"command not allowed: {argv[:1]}")
    out = _run(["pct", "exec", str(guest_id), "--", *argv])
    return {"guest": guest_id, "argv": argv, "stdout": out}
