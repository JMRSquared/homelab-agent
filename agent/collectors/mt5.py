"""MT5 (VM 200) heartbeat staleness and position state.

The signal here is *age*, not time. Every row of the TazzieBot export
carries a timestamp that moves on every reading, and `hostctl`'s
`/mt5/status` helpfully recomputes `age_s` on every call - so the raw
numbers change every single tick even when MetaTrader is perfectly
healthy and nothing happened. Feeding either of those into the diff is the
wake storm in a new costume.

So age is computed at collection time and bucketed (`fresh`,
`stale_gt_5m`, `stale_gt_30m`, `stale_gt_6h`); the bucket is what the diff
compares, and the raw seconds stay in the state for the model to read once
it has been woken for some other reason. Age between heartbeats only
increases, so a bucket boundary is crossed once on the way out and once on
the way back (a heartbeat landing again, which is a real recovery) - it
cannot oscillate the way a load average does, which is why a bucket here
needs no deadband on top.

Equity and balance are deliberately not collected at all: they move with
every price tick while a position is open, they are worth nothing as a
wake trigger, and `mt5_status` already answers them on demand.

Position state (how many positions are open, whether the terminal and EA
are allowed to trade, whether the terminal is connected) is not
continuously varying, so it is compared exactly - a position opening or
closing should wake the model.
"""

from types import MappingProxyType
from typing import Any

from agent.collectors.bands import threshold_bucket
from agent.collectors.registry import (
    Collector,
    FieldPolicy,
    mapping_projector,
    register,
    safe,
)
from agent.tools import infra

KEY = "mt5"

AGE_BUCKETS: tuple[tuple[float, str], ...] = (
    (300.0, "fresh"),
    (1800.0, "stale_gt_5m"),
    (21600.0, "stale_gt_30m"),
)
AGE_ABOVE = "stale_gt_6h"

POLICY = FieldPolicy(
    # The raw ages and the failure text are for the model's eyes, never the
    # diff's: both change shape or value on their own between ticks.
    excluded=frozenset({"heartbeat_age_s", "equity_age_s", "reason"}),
    exact=frozenset(
        {
            "available",
            "heartbeat_age",
            "equity_age",
            "open_positions",
            "connected",
            "terminal_trade_allowed",
            "ea_trade_allowed",
        }
    ),
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _total_heartbeat_age_s(row: dict[str, Any]) -> float | None:
    """How stale the EA's heartbeat actually is, end to end.

    Two independent delays stack: `age_s` is how long ago this row landed
    in the export DB, and `hb_age_s` is how old MetaTrader said the
    heartbeat file already was when that row was written. Reporting only
    one of them understates staleness by the other, and the live host has
    shown both being large at once.
    """
    row_age = _number(row.get("age_s"))
    file_age = _number(row.get("hb_age_s"))
    if row_age is None and file_age is None:
        return None
    return (row_age or 0.0) + (file_age or 0.0)


def _unavailable(reason: str) -> dict[str, Any]:
    """A stable shape for "we could not read the export at all".

    Stable matters: the reason text (an exception string) is excluded from
    the diff, so a missing or unreadable export registers once when it
    first goes missing and then stays quiet, rather than waking the model
    every minute with a slightly different error string.
    """
    return {KEY: {"available": False, "reason": reason}}


def collect() -> dict[str, Any]:
    raw = safe(infra.mt5_status)
    if "error" in raw:
        return _unavailable(str(raw["error"]))
    latest = raw.get("latest")
    positions = raw.get("latest_with_open_positions")
    if not isinstance(latest, dict) and not isinstance(positions, dict):
        return _unavailable("the TazzieBot export has no rows")

    equity_age_s = _number(latest.get("age_s")) if isinstance(latest, dict) else None
    heartbeat_age_s = _total_heartbeat_age_s(positions) if isinstance(positions, dict) else None
    row = positions if isinstance(positions, dict) else {}
    return {
        KEY: {
            "available": True,
            "heartbeat_age_s": heartbeat_age_s,
            "heartbeat_age": threshold_bucket(heartbeat_age_s, AGE_BUCKETS, above=AGE_ABOVE),
            "equity_age_s": equity_age_s,
            "equity_age": threshold_bucket(equity_age_s, AGE_BUCKETS, above=AGE_ABOVE),
            "open_positions": row.get("open_positions"),
            "connected": row.get("connected"),
            "terminal_trade_allowed": row.get("terminal_trade_allowed"),
            "ea_trade_allowed": row.get("ea_trade_allowed"),
        }
    }


COLLECTOR = register(
    Collector(
        name="mt5",
        keys=(KEY,),
        collect=collect,
        projectors=MappingProxyType({KEY: mapping_projector(POLICY)}),
        policies=MappingProxyType({KEY: POLICY}),
    )
)
