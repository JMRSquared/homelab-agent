"""The 60-second sweep: collect homelab state, diff it, wake the model on change.

An idle tick makes no model call — the daemon must not narrate an unchanged
server into Slack every minute. When the model provider is unreachable, the
tick keeps collecting and snapshotting state, queues diffs to SQLite instead
of losing them, and posts one plain (non-model) degraded alert rather than
one per outage minute. The backlog drains automatically once the provider
answers again.
"""

import json
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


def _round_band(value: Any, step: float) -> Any:
    """Round a number to the nearest multiple of `step`, for diff purposes.

    Leaves non-numeric input untouched so a collector's `{"error": ...}`
    shape (or any other unexpected value) passes through rather than
    raising.
    """
    if not isinstance(value, int | float):
        return value
    return round(value / step) * step


def _project_host(host: Any) -> Any:
    """Project `host_metrics()` output onto what's worth diffing on.

    `uptime_s` is strictly increasing every tick and must never be compared
    at all — it is dropped entirely rather than banded. `load1`,
    `mem_used_gb`, and `arc_gb` move constantly at full float precision, so a
    tick-to-tick reading is banded coarsely enough that ordinary drift
    (a load average wobbling by a few hundredths, memory by a few hundred
    MB) does not register as a "change". A real move still crosses a band
    boundary and is still reported.
    """
    if not isinstance(host, dict) or "error" in host:
        return host
    projected = {k: v for k, v in host.items() if k != "uptime_s"}
    if "load1" in projected:
        projected["load1"] = _round_band(projected["load1"], 0.5)
    if "mem_used_gb" in projected:
        projected["mem_used_gb"] = _round_band(projected["mem_used_gb"], 1.0)
    if "arc_gb" in projected:
        projected["arc_gb"] = _round_band(projected["arc_gb"], 1.0)
    return projected


def _project_zfs_datasets(datasets: Any) -> Any:
    """Band each dataset's `usedbysnapshots_gb` for diffing.

    Snapshot *counts* change rarely and are a meaningful signal, so they are
    left exact. `usedbysnapshots_gb` moves at byte precision as copy-on-write
    blocks accumulate behind existing snapshots and would otherwise become
    another always-changing field, exactly like `host.load1` before it was
    banded above.
    """
    if not isinstance(datasets, list):
        return datasets
    projected = []
    for entry in datasets:
        if not isinstance(entry, dict):
            projected.append(entry)
            continue
        item = dict(entry)
        if "usedbysnapshots_gb" in item:
            item["usedbysnapshots_gb"] = _round_band(item["usedbysnapshots_gb"], 1.0)
        projected.append(item)
    return projected


def _diff_projection(state: dict[str, Any]) -> dict[str, Any]:
    """Project raw `collect()` output onto the fields worth diffing on.

    The full, unprojected values (real uptime, precise load, exact snapshot
    usage) still go to the model in the prompt's `state` field — only the
    *comparison* that decides whether to wake the model at all uses this
    projection.
    """
    projected = dict(state)
    if "host" in projected:
        projected["host"] = _project_host(projected["host"])
    if "zfs_datasets" in projected:
        projected["zfs_datasets"] = _project_zfs_datasets(projected["zfs_datasets"])
    return projected


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for key, value in node.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: json.dumps(node, sort_keys=True)}


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    if old is None:
        return {"changed": [], "details": {}}
    a, b = _flatten(_diff_projection(old)), _flatten(_diff_projection(new))
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {"changed": changed, "details": {k: {"was": a.get(k), "now": b.get(k)} for k in changed}}


def _summarize_backlog(backlog: list[dict[str, Any]], total_pending: int) -> dict[str, Any]:
    """Coalesce a queued-diff backlog into a story, not a verbatim replay.

    A long outage can queue hundreds of diffs; dumping each one verbatim
    into the prompt wastes context on repetition and, if that oversized
    prompt itself fails, re-queues the whole backlog every tick — the
    opposite of "the queue drains when the provider returns". This instead
    reports, per changed key, how many times it moved and its first and most
    recent value — the shape of the outage, not a transcript of it.
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
        state = collect()
        previous = self._store.last_snapshot()
        self._store.put_snapshot(state)
        delta = diff(previous, state)
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
            # queued. Only this tick's new delta needs adding.
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
