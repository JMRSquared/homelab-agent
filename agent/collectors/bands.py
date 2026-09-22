"""Numeric banding, deadbands, and bucketing - the machinery that keeps
continuously-varying values out of the tick's diff.

Lifted verbatim out of `agent/tick.py` (behaviour unchanged) so every
collector reaches for the same treatment instead of inventing its own.
The long explanations here are the reason the diff works; they were paid
for with two live reproductions on the owner's host.
"""

import re
from collections.abc import Sequence
from typing import Any


def round_band(value: Any, step: float) -> Any:
    """Round a number to the nearest multiple of `step`. Leaves non-numeric
    input untouched so a collector's `{"error": ...}` shape passes through
    rather than raising."""
    if not isinstance(value, int | float):
        return value
    return round(value / step) * step


def band_with_deadband(raw: Any, previous_reported: Any, step: float) -> Any:
    """Band `raw` to the nearest multiple of `step`, but only move off
    `previous_reported` once `raw` clears it by more than a full step.

    Plain per-call rounding (`round_band` alone) has no memory: a value
    that happens to sit near a band edge (arc_gb parked close to arc_max,
    load1 idling near 0.75) can cross that edge on ordinary noise every
    single tick, flipping the reported band back and forth forever - the
    same wake-storm failure mode as the original unbanded fields, just
    happening at the edge instead of everywhere.

    A half-step deadband does not fix this: it only moves the flip point
    from the band's own edge to a point halfway between bands, and a noisy
    value parked near *that* point (confirmed live: real load1 readings
    0.69/0.76/0.65 around the 0.5/1.0 band edge, 20s apart) flips on it just
    as reliably. A full step is what actually gives the reported value
    inertia: `raw` has to clear the *entire* distance to the next band,
    not half of it, before the report moves - so a value bouncing within
    one step of its last reported position, on either side of any boundary
    in between, reports nothing. Comparing against the previously
    *reported* value instead of re-deriving fresh from the raw value each
    time is what makes that comparison possible at all.
    """
    if not isinstance(raw, int | float):
        return raw
    if isinstance(previous_reported, int | float) and abs(raw - previous_reported) <= step:
        return previous_reported
    return round(raw / step) * step


def threshold_bucket(
    value: float | None,
    thresholds: Sequence[tuple[float, str]],
    *,
    above: str,
    unknown: str = "unknown",
) -> str:
    """Map a number onto a coarse label: the first `(limit, label)` whose
    `limit` the value does not exceed, else `above`.

    This is banding for a quantity that only ever moves in one direction
    between real events - a heartbeat's age, a certificate's days
    remaining. Such a value crosses each boundary exactly once on the way
    down (or up) and cannot oscillate across it the way load average does,
    so a plain bucket needs no deadband to be quiet: it changes when the
    thing genuinely crossed a threshold, and it snaps back only when the
    underlying condition was genuinely repaired (a heartbeat landed, a
    certificate was renewed) - both of which are events worth a wake.
    """
    if value is None:
        return unknown
    for limit, label in thresholds:
        if value <= limit:
            return label
    return above


# ZFS list's human-readable size suffixes (binary, base 1024). No "i" and no
# trailing "B" is guaranteed, so both are optional.
_SIZE_RE = re.compile(r"^([\d.]+)\s*([KMGTPE]?)I?B?$", re.IGNORECASE)
_SIZE_UNIT_BYTES: dict[str, float] = {
    "": 1.0,
    "K": 1024.0,
    "M": 1024.0**2,
    "G": 1024.0**3,
    "T": 1024.0**4,
    "P": 1024.0**5,
    "E": 1024.0**6,
}


def zfs_size_to_gb(value: Any) -> float | None:
    """Parse a `zfs list` human-readable size string ("10.5G", "692G",
    "0B") into GB. Returns None (leave untouched) for anything that doesn't
    match, rather than raising."""
    if not isinstance(value, str):
        return None
    match = _SIZE_RE.match(value.strip())
    if not match:
        return None
    number, unit = match.groups()
    try:
        raw = float(number)
    except ValueError:
        return None
    return raw * _SIZE_UNIT_BYTES[unit.upper()] / _SIZE_UNIT_BYTES["G"]
