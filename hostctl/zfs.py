import datetime as dt
import subprocess

POOL = "tank"

# A snapshot with the same dataset + label prefix can't be retaken within
# this window. zfs_snapshot is model-callable, timestamped to the second so
# calls never dedupe, and the daemon prompt tells the model to snapshot
# before every risky change - without a limit that's unbounded snapshot
# creation on the pool holding the only copy of 692GB of family photos, with
# no destroy verb anywhere to reclaim the space.
RATE_LIMIT = dt.timedelta(hours=6)


class SnapshotRateLimitError(RuntimeError):
    """A snapshot with this dataset/label prefix was taken too recently."""


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout


def _check_dataset(dataset: str) -> None:
    if dataset != POOL and not dataset.startswith(f"{POOL}/"):
        raise ValueError(f"dataset must be {POOL!r} or under it, got {dataset!r}")


def status() -> dict[str, object]:
    pool = _run(["zpool", "status", "-v", POOL])
    listing = _run(["zfs", "list", "-H", "-o", "name,used,avail,refer", "-t", "filesystem"])
    datasets = [
        dict(zip(("name", "used", "avail", "refer"), line.split("\t"), strict=True))
        for line in listing.strip().splitlines()
        if line
    ]

    # Machine-readable (-p, raw bytes) pass for usedbysnapshots specifically,
    # so it can be reported as a precise number - the human-readable listing
    # above prints things like "10.5G", which the caller wants to keep for
    # readability elsewhere.
    raw_usage = _run(["zfs", "list", "-H", "-p", "-o", "name,usedbysnapshots", "-t", "filesystem"])
    used_by_snapshots_gb: dict[str, float] = {}
    for line in raw_usage.strip().splitlines():
        if not line:
            continue
        name, used_bytes = line.split("\t")
        used_by_snapshots_gb[name] = round(int(used_bytes) / 1_073_741_824, 2)

    snap_listing = _run(["zfs", "list", "-H", "-o", "name", "-t", "snapshot"])
    snapshot_counts: dict[str, int] = {}
    for line in snap_listing.strip().splitlines():
        if not line or "@" not in line:
            continue
        dataset_name = line.split("@", 1)[0]
        snapshot_counts[dataset_name] = snapshot_counts.get(dataset_name, 0) + 1

    for entry in datasets:
        name = entry["name"]
        entry["usedbysnapshots_gb"] = used_by_snapshots_gb.get(name, 0.0)  # type: ignore[assignment]
        entry["snapshot_count"] = snapshot_counts.get(name, 0)  # type: ignore[assignment]

    return {"pool_status": pool, "datasets": datasets}


def _recent_snapshot_times(dataset: str, label: str) -> list[dt.datetime]:
    prefix = f"{dataset}@{label}-"
    out = _run(
        ["zfs", "list", "-H", "-p", "-o", "name,creation", "-t", "snapshot", "-r", dataset]
    )
    times: list[dt.datetime] = []
    for line in out.strip().splitlines():
        if not line:
            continue
        name, creation = line.split("\t")
        if name.startswith(prefix):
            times.append(dt.datetime.fromtimestamp(int(creation), tz=dt.UTC))
    return times


def snapshot(dataset: str, label: str) -> str:
    _check_dataset(dataset)
    now = dt.datetime.now(dt.UTC)
    cutoff = now - RATE_LIMIT
    recent = [t for t in _recent_snapshot_times(dataset, label) if t >= cutoff]
    if recent:
        last = max(recent)
        raise SnapshotRateLimitError(
            f"a {label!r} snapshot of {dataset} was already taken at "
            f"{last.isoformat()}, within the last {RATE_LIMIT}; wait before retrying"
        )
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    name = f"{dataset}@{label}-{stamp}"
    _run(["zfs", "snapshot", name])
    return name


def scrub() -> dict[str, str]:
    _run(["zpool", "scrub", POOL])
    return {"pool": POOL, "scrub": "started"}
