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


def _raw_guests() -> list[Guest]:
    out: list[Guest] = []
    for kind, cmd in (("lxc", "pct"), ("qemu", "qm")):
        rows = json.loads(_run([cmd, "list", "--output-format", "json"]))
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
