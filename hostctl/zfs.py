import datetime as dt
import subprocess

POOL = "tank"


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout


def status() -> dict[str, object]:
    pool = _run(["zpool", "status", "-v", POOL])
    listing = _run(["zfs", "list", "-H", "-o", "name,used,avail,refer", "-t", "filesystem"])
    datasets = [
        dict(zip(("name", "used", "avail", "refer"), line.split("\t"), strict=True))
        for line in listing.strip().splitlines()
    ]
    return {"pool_status": pool, "datasets": datasets}


def snapshot(dataset: str, label: str) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    name = f"{dataset}@{label}-{stamp}"
    _run(["zfs", "snapshot", name])
    return name


def scrub() -> dict[str, str]:
    _run(["zpool", "scrub", POOL])
    return {"pool": POOL, "scrub": "started"}
