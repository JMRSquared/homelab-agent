"""ZFS pool health and per-dataset usage.

One hostctl call feeds two state keys with very different diff treatment:
`zfs_pool` is raw `zpool status -v` text reduced to three lines, and
`zfs_datasets` is a list of dataset rows whose sizes move constantly.
"""

import re
from types import MappingProxyType
from typing import Any

from agent.collectors.bands import band_with_deadband, zfs_size_to_gb
from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    Projector,
    on_sweep_start,
    register,
    safe,
)
from agent.tools import infra

POOL_KEY = "zfs_pool"
DATASETS_KEY = "zfs_datasets"

DATASET_POLICY = FieldPolicy(
    excluded=frozenset({"refer"}),
    banded=MappingProxyType({"used": 1.0, "avail": 1.0, "usedbysnapshots_gb": 1.0}),
    exact=frozenset({"name", "snapshot_count"}),
)

# The only lines of `zpool status -v`'s raw text that make it into the diff.
# Everything else - the config: device table, the scan: line's numeric
# scanned/rate/percent/ETA continuation, the pool: name line - is dropped.
POOL_KEPT_KEYS = frozenset({"state", "errors", "scan"})
POOL_POLICY = FieldPolicy(exact=POOL_KEPT_KEYS)


_sweep_cache: dict[str, dict[str, Any]] = {}


@on_sweep_start
def _reset_cache() -> None:
    _sweep_cache.clear()


def fetch() -> dict[str, Any]:
    """One `/zfs/status` call per sweep, shared with the backup-coverage
    collector, which needs the same dataset listing. Cleared by the
    registry at the start of every sweep, so it is a within-sweep cache,
    never a stale view."""
    if "zfs" not in _sweep_cache:
        _sweep_cache["zfs"] = safe(infra.zfs_report)
    return _sweep_cache["zfs"]


def collect() -> dict[str, Any]:
    zfs = fetch()
    # Explicit, not `.get(key, zfs)`: that fallback silently substituted the
    # *entire* raw hostctl response (including a successful response missing
    # the key by mistake) in place of one field, which would show up as an
    # enormous spurious diff. An error stays an error in both slots; success
    # reads the two keys the response is documented to have.
    if "error" in zfs:
        return {POOL_KEY: zfs, DATASETS_KEY: zfs}
    return {POOL_KEY: zfs.get("pool_status"), DATASETS_KEY: zfs.get("datasets")}


def project_datasets(datasets: Any, previous: Any) -> Any:
    """Project `zfs.status()`'s dataset list onto what's worth diffing on.

    `used` and `avail` are `zfs list` size strings at three significant
    figures - `avail` is pool-wide, so writing one GB anywhere on the pool
    changes it (and therefore every dataset row that reports it) on every
    tick. Both are converted to GB and banded with hysteresis, matched
    dataset-by-dataset (by `name`) against the previous tick's reported
    values. `usedbysnapshots_gb` gets the same treatment - it moves at byte
    precision as copy-on-write blocks accumulate. `refer` is dropped
    entirely: it's derivable and adds no signal `used` doesn't already
    carry. `snapshot_count` changes rarely and is left exact - it's a
    meaningful signal, not noise.
    """
    if not isinstance(datasets, list):
        return datasets
    prev_by_name: dict[str, dict[str, Any]] = {
        entry["name"]: entry
        for entry in (previous if isinstance(previous, list) else [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    projected: list[Any] = []
    for entry in datasets:
        if not isinstance(entry, dict):
            projected.append(entry)
            continue
        name = entry.get("name")
        prev_entry = prev_by_name.get(name, {}) if isinstance(name, str) else {}
        item: dict[str, Any] = {}
        for key, value in entry.items():
            if key in DATASET_POLICY.excluded:
                continue
            step = DATASET_POLICY.banded.get(key)
            if step is None:
                item[key] = value
            elif key in ("used", "avail"):
                size_gb = zfs_size_to_gb(value)
                item[key] = (
                    band_with_deadband(size_gb, prev_entry.get(key), step)
                    if size_gb is not None
                    else value
                )
            else:
                item[key] = band_with_deadband(value, prev_entry.get(key), step)
        projected.append(item)
    return projected


_SCAN_VERB_RE = re.compile(
    r"^(scrub|resilver)\s+(in progress|repaired|completed|cancelled)", re.IGNORECASE
)


def normalize_scan_line(value: str) -> str:
    """Reduce a `zpool status` scan: line to its verb, dropping the scanned
    bytes/rate/percent/ETA that moves every second during a scrub or
    resilver (Debian's zfsutils-linux ships a monthly scrub cron; the pool's
    last scrub ran over three hours)."""
    match = _SCAN_VERB_RE.match(value)
    if match:
        return f"{match.group(1)} {match.group(2)}".lower()
    return value.split(",")[0].strip()


def project_pool(pool_status: Any, _previous: Any = None) -> Any:
    """Project `zpool status -v`'s raw text onto what's worth diffing on:
    the `state:` line, the `errors:` line, and the `scan:` line normalized
    to its verb (see `normalize_scan_line`). The config: device table and
    everything else in the text is dropped - a pool-level state change is
    what matters for the diff; the full text still reaches the model via
    the prompt's `state`."""
    if not isinstance(pool_status, str):
        return pool_status
    projected: dict[str, str] = {}
    for line in pool_status.splitlines():
        stripped = line.strip()
        if stripped.startswith("state:"):
            projected["state"] = stripped
        elif stripped.startswith("errors:"):
            projected["errors"] = stripped
        elif stripped.startswith("scan:"):
            projected["scan"] = normalize_scan_line(stripped[len("scan:") :].strip())
    return projected


_PROJECTORS: dict[str, Projector] = {POOL_KEY: project_pool, DATASETS_KEY: project_datasets}

COLLECTOR = register(
    Collector(
        name="zfs",
        keys=(POOL_KEY, DATASETS_KEY),
        collect=collect,
        projectors=MappingProxyType(_PROJECTORS),
        policies=MappingProxyType({POOL_KEY: POOL_POLICY, DATASETS_KEY: DATASET_POLICY}),
    )
)
