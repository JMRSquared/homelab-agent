"""TLS certificate expiry, keyed by the target name hostctl checks.

`days_remaining` is a countdown: left in the diff raw it would wake the
model once a day per certificate at best, and on every tick if hostctl
reports it fractionally. Only a threshold crossing is material, so the
raw number is excluded and a bucket derived from it
(`>30d` / `<=30d` / `<=14d` / `<=7d` / `<=1d` / `expired`) is what gets
compared. A countdown only moves one way until a renewal, so the bucket
steps down once per threshold and jumps back to `>30d` when the
certificate is actually renewed - both real events.

hostctl's `/certs/status` does the checking (a live TLS handshake per
configured target). This collector only decides what of that is worth
waking a model for. On an older hostctl without the route, on an
unreachable hostctl, or with no targets configured, it produces *nothing
at all* - not an error marker, not an empty key - so none of those can
crash the tick or wake the model. The rest of the sweep already reports
loudly when hostctl itself is unreachable; this collector has no business
saying it twice.

A target hostctl could not reach is reported with `ok: false` and its
failure text excluded from the diff: the text varies between a refused
connection, a timeout and a handshake failure, and a value that changes
shape between ticks is exactly what must not reach the comparison. That
text is carried as `unreachable_reason` rather than `error`, which is
reserved across this package for "this whole collector failed" - a state
key holding a bare `error` is passed through the projection untouched by
design, so borrowing the name here would have quietly disabled every
exclusion on the key.
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
ROUTE = "/certs/status"

DAY_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.0, "expired"),
    (1.0, "lte_1d"),
    (7.0, "lte_7d"),
    (14.0, "lte_14d"),
    (30.0, "lte_30d"),
)
DAYS_ABOVE = "gt_30d"

POLICY = FieldPolicy(
    excluded=frozenset({"days_remaining", "unreachable_reason"}),
    exact=frozenset({"ok", "subject", "issuer", "not_after", "expiry"}),
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
    entries = data.get("certs")
    if not isinstance(entries, list):
        return {}
    now = dt.datetime.now(dt.UTC)
    certs: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        reachable = bool(entry.get("ok"))
        days = _days_remaining(entry, now) if reachable else None
        certs[name] = {
            "ok": reachable,
            "subject": entry.get("subject"),
            "issuer": entry.get("issuer"),
            "not_after": entry.get("not_after"),
            "days_remaining": None if days is None else round(days, 2),
            "expiry": threshold_bucket(days, DAY_BUCKETS, above=DAYS_ABOVE),
            "unreachable_reason": entry.get("error"),
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
