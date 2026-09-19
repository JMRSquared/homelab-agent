"""The 60-second sweep: collect homelab state, diff it, wake the model on change.

An idle tick makes no model call — the daemon must not narrate an unchanged
server into Slack every minute. When the model provider is unreachable, the
tick keeps collecting and snapshotting state, queues diffs to SQLite instead
of losing them, and posts one plain (non-model) degraded alert rather than
one per outage minute. The backlog drains automatically once the provider
answers again.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agent.store import Store
from agent.tools import infra

SYSTEM_DAEMON = (
    "You are the autonomous operator of Tech's home server. "
    "You have just been woken because the homelab state changed. "
    "Diagnose the change and fix it yourself using your tools. Do not ask permission. "
    "Post what you did and why to #homelab using slack_say, in this shape:\n"
    ":wrench: <what you did>\n  why: <evidence>\n  result: <outcome>\n"
    "If nothing needs doing, call no tools and reply with the single word: idle. "
    "Snapshot a dataset before any change that touches its contents. "
    "You have no access to the trading VM: guest 200 (mt5) is permanently out of "
    "scope, every tool that could reach it rejects the request, and you must never "
    "attempt it."
)

# Cap on how many queued outage diffs get replayed to the model verbatim in
# one tick (see `_summarize_backlog`). A long outage can queue hundreds of
# rows; a 720-row backlog blown into one prompt is itself likely to fail,
# which would re-queue all 720 rows plus the new one forever.
BACKLOG_LIMIT = 50


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


def _safe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a single collector, turning a transport failure into data.

    hostctl or a service being down must not crash the whole tick — that is
    exactly when the agent needs to keep watching. A failed collector reports
    itself as `{"error": ...}` instead, which both keeps the tick alive and
    shows up as a real, diffable state change on the next comparison.
    """
    try:
        return fn()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect() -> dict[str, Any]:
    """Collect current homelab state. Blocking - see `collect_async`.

    Three blocking `httpx` calls (guests_list, zfs_report, host_metrics)
    live underneath `_safe`. Kept synchronous so it can still be called
    directly (as the test suite and `_safe`'s docstring assume); callers
    inside the async daemon path must go through `collect_async` instead.
    """
    guests = _safe(infra.guests_list)
    zfs = _safe(infra.zfs_report)
    host = _safe(infra.host_metrics)
    # Explicit, not `.get(key, zfs)`: that fallback silently substituted the
    # *entire* raw hostctl response (including a successful response missing
    # the key by mistake) in place of one field, which would show up as an
    # enormous spurious diff. An error stays an error in both slots; success
    # reads the two keys the response is documented to have.
    if "error" in zfs:
        zfs_pool: Any = zfs
        zfs_datasets: Any = zfs
    else:
        zfs_pool = zfs.get("pool_status")
        zfs_datasets = zfs.get("datasets")
    return {
        "guests": (
            {str(g["id"]): g["status"] for g in guests["guests"]}
            if "guests" in guests
            else guests
        ),
        "zfs_pool": zfs_pool,
        "zfs_datasets": zfs_datasets,
        "host": host,
    }


async def collect_async() -> dict[str, Any]:
    """`collect()`, off the event loop.

    `collect()`'s three hostctl calls are blocking `httpx` requests with
    their own multi-second timeouts; run inline inside `Ticker.once`'s
    coroutine, a hung hostctl stalls the whole event loop - the Slack
    websocket, every family reply, and the I1 asyncio.to_thread dispatch
    offload all share this one loop. Same treatment as I1's fix to
    `agent/model.py`: push the blocking call to a thread.
    """
    return await asyncio.to_thread(collect)


# --- Diff policy: every leaf key collect() can produce, and how it enters
# the diff. Kept as explicit sets (not "whatever collect() happens to
# return") so a test can enumerate a realistic sample and assert every key
# is accounted for in one of them - a field collect() starts returning that
# isn't listed anywhere here fails that test loudly, instead of silently
# entering the diff unbanded the way host.uptime_s and host.load1 originally
# did (see tests/agent/test_tick.py).

HOST_EXCLUDED = frozenset({"uptime_s"})
# load1 stays in the diff, banded, rather than joining uptime_s as excluded.
# It's the noisiest field here by a wide margin, but it is also the one
# signal that catches a runaway process pinning the host's CPU when nothing
# else has - guests, zfs_pool's state, and a monitor going down all report
# on symptoms downstream of that, sometimes minutes later, sometimes not at
# all (a busy but not-yet-failing process). Dropping it trades a real,
# distinct incident class for less banding work. With the full-step
# deadband below it doesn't need dropping: fed the real oscillating sample
# that broke the half-step version (0.69/0.76/0.65/0.78/0.71, 20s apart,
# straddling the 0.5/1.0 edge), it now reports nothing across any
# consecutive pair, and a sustained climb to 3.0 still reports once - see
# tests/agent/test_tick.py.
HOST_BANDED = frozenset({"load1", "mem_used_gb", "arc_gb"})
HOST_PASSTHROUGH = frozenset({"mem_total_gb"})
_HOST_BAND_STEP: dict[str, float] = {"load1": 0.5, "mem_used_gb": 1.0, "arc_gb": 1.0}

DATASET_EXCLUDED = frozenset({"refer"})
DATASET_BANDED = frozenset({"used", "avail", "usedbysnapshots_gb"})
DATASET_PASSTHROUGH = frozenset({"name", "snapshot_count"})

# The only lines of `zpool status -v`'s raw text that make it into the diff.
# Everything else - the config: device table, the scan: line's numeric
# scanned/rate/percent/ETA continuation, the pool: name line - is dropped.
ZFS_POOL_KEPT_KEYS = frozenset({"state", "errors", "scan"})


def _round_band(value: Any, step: float) -> Any:
    """Round a number to the nearest multiple of `step`. Leaves non-numeric
    input untouched so a collector's `{"error": ...}` shape passes through
    rather than raising."""
    if not isinstance(value, int | float):
        return value
    return round(value / step) * step


def _band_with_deadband(raw: Any, previous_reported: Any, step: float) -> Any:
    """Band `raw` to the nearest multiple of `step`, but only move off
    `previous_reported` once `raw` clears it by more than a full step.

    Plain per-call rounding (`_round_band` alone) has no memory: a value
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


def _project_host(host: Any, previous: Any) -> Any:
    """Project `host_metrics()` output onto what's worth diffing on.

    `uptime_s` is strictly increasing every tick and must never be compared
    at all - dropped entirely, not banded. `load1`, `mem_used_gb`, and
    `arc_gb` are banded with hysteresis (see `_band_with_deadband`) against
    their previously *reported* value. Everything else passes through
    unchanged (exact match).
    """
    if not isinstance(host, dict) or "error" in host:
        return host
    prev = previous if isinstance(previous, dict) else {}
    projected: dict[str, Any] = {}
    for key, value in host.items():
        if key in HOST_EXCLUDED:
            continue
        if key in HOST_BANDED:
            projected[key] = _band_with_deadband(value, prev.get(key), _HOST_BAND_STEP[key])
        else:
            projected[key] = value
    return projected


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


def _zfs_size_to_gb(value: Any) -> float | None:
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


def _project_zfs_datasets(datasets: Any, previous: Any) -> Any:
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
    projected = []
    for entry in datasets:
        if not isinstance(entry, dict):
            projected.append(entry)
            continue
        name = entry.get("name")
        prev_entry = prev_by_name.get(name, {}) if isinstance(name, str) else {}
        item: dict[str, Any] = {}
        for key, value in entry.items():
            if key in DATASET_EXCLUDED:
                continue
            if key in ("used", "avail"):
                size_gb = _zfs_size_to_gb(value)
                item[key] = (
                    _band_with_deadband(size_gb, prev_entry.get(key), 1.0)
                    if size_gb is not None
                    else value
                )
            elif key == "usedbysnapshots_gb":
                item[key] = _band_with_deadband(value, prev_entry.get(key), 1.0)
            else:
                item[key] = value
        projected.append(item)
    return projected


_SCAN_VERB_RE = re.compile(
    r"^(scrub|resilver)\s+(in progress|repaired|completed|cancelled)", re.IGNORECASE
)


def _normalize_scan_line(value: str) -> str:
    """Reduce a `zpool status` scan: line to its verb, dropping the scanned
    bytes/rate/percent/ETA that moves every second during a scrub or
    resilver (Debian's zfsutils-linux ships a monthly scrub cron; the pool's
    last scrub ran over three hours)."""
    match = _SCAN_VERB_RE.match(value)
    if match:
        return f"{match.group(1)} {match.group(2)}".lower()
    return value.split(",")[0].strip()


def _project_zfs_pool(pool_status: Any) -> Any:
    """Project `zpool status -v`'s raw text onto what's worth diffing on:
    the `state:` line, the `errors:` line, and the `scan:` line normalized
    to its verb (see `_normalize_scan_line`). The config: device table and
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
            projected["scan"] = _normalize_scan_line(stripped[len("scan:") :].strip())
    return projected


def _diff_projection(
    state: dict[str, Any], previous_projection: dict[str, Any] | None
) -> dict[str, Any]:
    """Project raw `collect()` output onto the fields worth diffing on.

    The full, unprojected values (real uptime, precise load, exact scrub
    progress, exact snapshot usage) still go to the model in the prompt's
    `state` field - only the *comparison* that decides whether to wake the
    model at all uses this projection. `previous_projection`, when given, is
    what this function itself reported on a previous call - it's what gives
    the numeric bands a memory (see `_band_with_deadband`).
    """
    prev = previous_projection or {}
    projected = dict(state)
    if "host" in projected:
        projected["host"] = _project_host(projected["host"], prev.get("host"))
    if "zfs_datasets" in projected:
        projected["zfs_datasets"] = _project_zfs_datasets(
            projected["zfs_datasets"], prev.get("zfs_datasets")
        )
    if "zfs_pool" in projected:
        projected["zfs_pool"] = _project_zfs_pool(projected["zfs_pool"])
    return projected


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: json.dumps(node, sort_keys=True)}


def diff(
    old: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    old_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diff two raw `collect()`-shaped states.

    Compares projected (excluded/banded) versions of `old` and `new`, not
    the raw values - see `_diff_projection`. `old_projection`, when given, is
    the actual projection reported for `old` the last time it was compared
    (as persisted by `Ticker`), and is what gives the numeric bands a memory
    across many ticks: without it, a value sitting near a band edge is
    re-banded fresh from its own raw value on every call, which can flip
    back and forth on ordinary noise. `Ticker.once` threads this through
    from the stored snapshot; a bare two-argument call (as in most of this
    module's tests) has no such memory and bands `old` fresh from itself,
    which is still correct for a single-step comparison.
    """
    if old is None:
        return {"changed": [], "details": {}}
    projected_old = old_projection if old_projection is not None else _diff_projection(old, None)
    projected_new = _diff_projection(new, projected_old)
    a, b = _flatten(projected_old), _flatten(projected_new)
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {"changed": changed, "details": {k: {"was": a.get(k), "now": b.get(k)} for k in changed}}


def _summarize_backlog(backlog: list[dict[str, Any]], total_pending: int) -> dict[str, Any]:
    """Coalesce a queued-diff backlog into a story, not a verbatim replay.

    A long outage can queue hundreds of diffs; dumping each one verbatim
    into the prompt wastes context on repetition and, if that oversized
    prompt itself fails, re-queues the whole backlog every tick - the
    opposite of "the queue drains when the provider returns". This instead
    reports, per changed key, how many times it moved and its first and most
    recent value - the shape of the outage, not a transcript of it.
    """
    if not backlog:
        return {"queued_diffs": 0}
    touched: dict[str, dict[str, Any]] = {}
    for item in backlog:
        for key, detail in item.get("details", {}).items():
            entry = touched.setdefault(
                key, {"times_changed": 0, "first_was": detail.get("was")}
            )
            entry["times_changed"] += 1
            entry["last_now"] = detail.get("now")
    return {
        "queued_diffs": len(backlog),
        "total_pending": total_pending,
        "omitted_older_diffs": max(0, total_pending - len(backlog)),
        "changed_keys": touched,
    }


class Ticker:
    def __init__(
        self, agent: Runner, store: Store, notify: Callable[[str, str], Awaitable[None]]
    ) -> None:
        self._agent = agent
        self._store = store
        self._notify = notify
        self._degraded = False

    async def once(self) -> None:
        state = await collect_async()
        previous_record = self._store.last_snapshot()
        previous_state = previous_record.get("state") if previous_record else None
        previous_projection = previous_record.get("projection") if previous_record else None
        new_projection = _diff_projection(state, previous_projection)
        self._store.put_snapshot({"state": state, "projection": new_projection})
        delta = diff(previous_state, state, old_projection=previous_projection)
        total_pending = self._store.pending_count()
        # Peek, don't drain: rows are deleted only after a successful model
        # call below. Deleting first (the old behaviour) and re-queueing on
        # failure lost the backlog outright if the process died in between.
        backlog_rows = self._store.peek_pending(BACKLOG_LIMIT) if total_pending else []
        backlog = [item for _, item in backlog_rows]
        if not delta["changed"] and not backlog:
            return
        prompt = json.dumps(
            {
                "state": state,
                "change": delta,
                "backlog_summary": _summarize_backlog(backlog, total_pending),
            },
            indent=2,
            sort_keys=True,
        )
        try:
            answer = await self._agent.run(prompt, priority="daemon", system=SYSTEM_DAEMON)
        except Exception as exc:
            # The backlog was only peeked, never deleted, so it's still
            # queued. Only a *real* new change needs adding - a tick that
            # ran solely because the backlog was non-empty (delta empty,
            # unchanged server) must not queue another empty row on top;
            # that's a useless row a minute for the length of an outage.
            if delta["changed"]:
                self._store.queue_pending(delta)
            if not self._degraded:
                self._degraded = True
                await self._notify(
                    "#homelab",
                    f":warning: agent degraded, model provider unreachable ({exc}). "
                    "Still watching, changes are queued.",
                )
            return
        if backlog_rows:
            self._store.delete_pending([row_id for row_id, _ in backlog_rows])
        if self._degraded:
            self._degraded = False
            await self._notify("#homelab", ":white_check_mark: agent recovered, backlog drained.")
        self._store.record_event("tick", {"change": delta, "answer": answer})
