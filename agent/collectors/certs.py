"""TLS certificate expiry, keyed by subject.

`days_remaining` is a countdown: left in the diff raw it would wake the
model once a day per certificate at best, and on every tick if hostctl
reports it fractionally. Only a threshold crossing is material, so the
raw number is excluded and a bucket derived from it
(`>30d` / `<=30d` / `<=14d` / `<=7d` / `<=1d` / `expired`) is what gets
compared. A countdown only moves one way until a renewal, so the bucket
steps down once per threshold and jumps back to `>30d` when the
certificate is actually renewed - both real events.

The hostctl route that feeds this is being built separately (see the
report for the exact shape assumed). Until it exists this collector
produces *nothing at all* - not an error marker, not an empty key - so a
missing route can neither crash the tick nor wake the model. The rest of
the sweep already reports loudly when hostctl itself is unreachable; this
collector has no business saying it twice.
"""

import datetime as dt
from types import MappingProxyType
from typing import Any

from agent import clients
from agent.collectors.bands import threshold_bucket
from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    nested_mapping_projector,
    register,
)

KEY = "certs"
ROUTE = "/certs"

DAY_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.0, "expired"),
    (1.0, "lte_1d"),
    (7.0, "lte_7d"),
    (14.0, "lte_14d"),
    (30.0, "lte_30d"),
)
DAYS_ABOVE = "gt_30d"

POLICY = FieldPolicy(
    excluded=frozenset({"days_remaining"}),
    exact=frozenset({"not_after", "expiry"}),
)


def _days_remaining(entry: dict[str, Any], now: dt.datetime) -> float | None:
    raw = entry.get("days_remaining")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    not_after = entry.get("not_after")
    if not isinstance(not_after, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(not_after.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (parsed - now).total_seconds() / 86400.0


def collect() -> dict[str, Any]:
    try:
        data = clients.hostctl_get_optional(ROUTE)
    except Exception:
        # Any failure at all - not just the 404/connection cases
        # hostctl_get_optional already swallows - degrades to nothing.
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("certificates")
    if not isinstance(entries, list):
        return {}
    now = dt.datetime.now(dt.UTC)
    certs: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        subject = entry.get("subject")
        if not isinstance(subject, str) or not subject:
            continue
        days = _days_remaining(entry, now)
        certs[subject] = {
            "not_after": entry.get("not_after"),
            "days_remaining": None if days is None else round(days, 2),
            "expiry": threshold_bucket(days, DAY_BUCKETS, above=DAYS_ABOVE),
        }
    if not certs:
        return {}
    return {KEY: certs}


COLLECTOR = register(
    Collector(
        name="certs",
        keys=(KEY,),
        collect=collect,
        projectors=MappingProxyType({KEY: nested_mapping_projector(POLICY)}),
        policies=MappingProxyType({KEY: POLICY}),
    )
)
