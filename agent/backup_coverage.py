"""Off-site backup coverage: what is protected, what is not, and how much.

The spec asks the agent to nag about the missing off-site copy of
irreplaceable data. Nothing did. This is the part that can: it joins the
live ZFS dataset listing to the owner's classification policy and says,
per dataset, whether an off-site copy exists.

One rule shapes everything here: **a local ZFS snapshot is not a backup.**
A snapshot lives on the same pool, on the same disks, in the same house
as the data it snapshots. It protects against a bad delete and a bad
upgrade. It protects against nothing that takes the pool - a controller
failure, a second disk dying mid-resilver, a fire, a theft. A dataset
whose only protection is snapshots is reported here as unprotected, in
those words, however many snapshots it has. Counting them as coverage is
how people find out on the worst day that they never had a backup.

This module reports. It does not replicate anything.
"""

import datetime as dt
import json
import os
from typing import Any

from agent import backup_policy
from agent.collectors.bands import zfs_size_to_gb

# Per-dataset protection state, so the report can say when protection last
# changed rather than only what it is now. Small, rewritten only when the
# state actually differs - not once a minute for the life of the process.
DEFAULT_STATE_PATH = "/tank/dev/agent/backup-coverage.json"

PROTECTED = "offsite"
UNPROTECTED = "none"


def _state_path() -> str:
    return os.environ.get("AGENT_BACKUP_STATE", DEFAULT_STATE_PATH)


def _load_state() -> dict[str, Any]:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    """Best-effort persist. A read-only or missing directory loses the
    'changed at' history and nothing else - the coverage numbers
    themselves never depend on this file."""
    path = _state_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
    except OSError:
        return


def _size_gb(entry: dict[str, Any]) -> float:
    gb = zfs_size_to_gb(entry.get("used"))
    return round(gb, 2) if gb is not None else 0.0


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dataset_row(entry: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    name = str(entry.get("name", ""))
    classification, note = backup_policy.classification_of(policy, name)
    target = backup_policy.offsite_target(policy, name)
    snapshots = _int(entry.get("snapshot_count"))
    protection = PROTECTED if target else UNPROTECTED
    return {
        "classification": classification,
        "note": note,
        "protection": protection,
        "offsite_target": str(target.get("target", "")) if target else None,
        "size_gb": _size_gb(entry),
        "snapshot_count": snapshots,
        # Named for what it is, so no reader can mistake it for coverage.
        "snapshots_only": protection == UNPROTECTED and snapshots > 0,
    }


def _summary(rows: dict[str, dict[str, Any]], totals: dict[str, float]) -> str:
    unprotected = [n for n, r in rows.items() if _is_exposed(r)]
    if not unprotected:
        return "every irreplaceable dataset has a declared off-site copy."
    names = ", ".join(sorted(unprotected))
    return (
        f"{totals['irreplaceable_unprotected_gb']:.0f}G of irreplaceable data has no "
        f"off-site copy: {names}. Local snapshots on these datasets are not coverage - "
        "they sit on the same pool and die with it."
    )


def _is_exposed(row: dict[str, Any]) -> bool:
    return bool(
        row["classification"] == backup_policy.IRREPLACEABLE
        and row["protection"] == UNPROTECTED
    )


def report(
    datasets: list[dict[str, Any]],
    *,
    policy: dict[str, Any] | None = None,
    now: dt.datetime | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Coverage for a `zfs.status()`-shaped dataset listing.

    `persist` writes the protection-state file so `protection_changed_at`
    means something across restarts; tests and read-only callers can turn
    it off.
    """
    policy = policy or backup_policy.load_policy()
    now = now or dt.datetime.now(dt.UTC)
    stamp = now.isoformat()

    rows: dict[str, dict[str, Any]] = {}
    for entry in datasets:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        rows[str(entry["name"])] = _dataset_row(entry, policy)

    previous = _load_state()
    changed_at: dict[str, str] = {}
    next_state: dict[str, Any] = {}
    for name, row in rows.items():
        before = previous.get(name)
        was = before.get("protection") if isinstance(before, dict) else None
        since = before.get("since") if isinstance(before, dict) else None
        if was == row["protection"] and isinstance(since, str):
            changed_at[name] = since
        else:
            changed_at[name] = stamp
        next_state[name] = {"protection": row["protection"], "since": changed_at[name]}
    if persist and next_state != previous:
        _save_state(next_state)

    totals = {
        "irreplaceable_gb": 0.0,
        "irreplaceable_unprotected_gb": 0.0,
        "protected_gb": 0.0,
        "unclassified_gb": 0.0,
    }
    for row in rows.values():
        size = float(row["size_gb"])
        if row["classification"] == backup_policy.IRREPLACEABLE:
            totals["irreplaceable_gb"] += size
            if row["protection"] == UNPROTECTED:
                totals["irreplaceable_unprotected_gb"] += size
        if row["classification"] == backup_policy.UNKNOWN:
            totals["unclassified_gb"] += size
        if row["protection"] == PROTECTED:
            totals["protected_gb"] += size
    totals = {k: round(v, 2) for k, v in totals.items()}

    return {
        "generated_at": stamp,
        "datasets": rows,
        "totals": totals,
        "unprotected_irreplaceable": sorted(n for n, r in rows.items() if _is_exposed(r)),
        "unclassified": sorted(
            n for n, r in rows.items() if r["classification"] == backup_policy.UNKNOWN
        ),
        "protection_changed_at": changed_at,
        "snapshots_are_not_backups": (
            "ZFS snapshots listed here are on the same pool as the data and are not "
            "counted as coverage. Only a copy off this machine is."
        ),
        "summary": _summary(rows, totals),
    }
