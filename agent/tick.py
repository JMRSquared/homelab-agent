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
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from agent import collectors
from agent.collectors import bands
from agent.collectors import host as host_collector
from agent.collectors import zfs as zfs_collector
from agent.prompts import INCIDENT_MEMORY_GUIDANCE, MT5_GUARDRAILS, SKILLS_INDEX
from agent.store import Store

SYSTEM_DAEMON = (
    "You are the autonomous operator of Tech's home server. "
    "You have just been woken because the homelab state changed. "
    "Diagnose the change and fix it yourself using your tools. Do not ask permission. "
    "Post what you did and why to #homelab using slack_say, in this shape:\n"
    ":wrench: <what you did>\n  why: <evidence>\n  result: <outcome>\n"
    "If nothing needs doing, call no tools and reply with the single word: idle. "
    "Snapshot a dataset before any change that touches its contents. "
    + MT5_GUARDRAILS
    + INCIDENT_MEMORY_GUIDANCE
    + SKILLS_INDEX
)

# Cap on how many queued outage diffs get replayed to the model verbatim in
# one tick (see `_summarize_backlog`). A long outage can queue hundreds of
# rows; a 720-row backlog blown into one prompt is itself likely to fail,
# which would re-queue all 720 rows plus the new one forever.
BACKLOG_LIMIT = 50


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


def collect() -> dict[str, Any]:
    """Collect current homelab state. Blocking - see `collect_async`.

    Every source is a registered collector (see `agent/collectors/`), which
    declares both how to fetch its data and how that data enters the diff.
    The blocking `httpx` calls underneath live behind each collector's own
    `safe()` wrapper, so hostctl being down degrades to `{"error": ...}`
    per key rather than taking the tick with it. Kept synchronous so it can
    still be called directly (as the test suite assumes); callers inside
    the async daemon path must go through `collect_async` instead.
    """
    return collectors.collect_all()


async def collect_async() -> dict[str, Any]:
    """`collect()`, off the event loop.

    `collect()`'s hostctl calls are blocking `httpx` requests with their
    own multi-second timeouts; run inline inside `Ticker.once`'s
    coroutine, a hung hostctl stalls the whole event loop - the Slack
    websocket, every family reply, and the I1 asyncio.to_thread dispatch
    offload all share this one loop. Same treatment as I1's fix to
    `agent/model.py`: push the blocking call to a thread.
    """
    return await asyncio.to_thread(collect)


# --- Diff policy ---------------------------------------------------------
#
# Each collector declares, for every leaf key it can produce, how that key
# enters the diff: excluded entirely, banded with a deadband at a stated
# step, or compared exactly (see `agent/collectors/registry.py`'s
# `FieldPolicy`). Keeping that declaration next to the collector - rather
# than as a second, separate edit here - is what stops a newly added field
# entering the diff unbanded the way host.uptime_s and host.load1
# originally did, waking a fully autonomous model 1,440 times a day on an
# idle server.
#
# The names below are re-exported so the long-standing tick tests, and any
# caller that learned these names, keep working against one shared policy
# rather than a second copy of it.

HOST_EXCLUDED = host_collector.POLICY.excluded
HOST_BANDED = frozenset(host_collector.POLICY.banded)
HOST_PASSTHROUGH = host_collector.POLICY.exact
_HOST_BAND_STEP: dict[str, float] = dict(host_collector.POLICY.banded)

DATASET_EXCLUDED = zfs_collector.DATASET_POLICY.excluded
DATASET_BANDED = frozenset(zfs_collector.DATASET_POLICY.banded)
DATASET_PASSTHROUGH = zfs_collector.DATASET_POLICY.exact

ZFS_POOL_KEPT_KEYS = zfs_collector.POOL_KEPT_KEYS

_round_band = bands.round_band
_band_with_deadband = bands.band_with_deadband
_zfs_size_to_gb = bands.zfs_size_to_gb
_project_host = host_collector.COLLECTOR.projectors[host_collector.KEY]
_project_zfs_datasets = zfs_collector.project_datasets
_normalize_scan_line = zfs_collector.normalize_scan_line


def _project_zfs_pool(pool_status: Any) -> Any:
    return zfs_collector.project_pool(pool_status, None)


def _diff_projection(
    state: dict[str, Any], previous_projection: dict[str, Any] | None
) -> dict[str, Any]:
    """Project raw `collect()` output onto the fields worth diffing on.

    The full, unprojected values (real uptime, precise load, exact scrub
    progress, exact snapshot usage, the raw MT5 heartbeat age) still go to
    the model in the prompt's `state` field - only the *comparison* that
    decides whether to wake the model at all uses this projection.
    `previous_projection`, when given, is what this function itself
    reported on a previous call - it's what gives the numeric bands a
    memory (see `bands.band_with_deadband`).

    The projection itself is assembled from the registry: each collector
    supplies the projector for the keys it owns, and a key with no
    registered projector is compared exactly.
    """
    return collectors.project_all(state, previous_projection)


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
