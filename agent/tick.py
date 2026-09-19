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
    return {
        "guests": (
            {str(g["id"]): g["status"] for g in guests["guests"]}
            if "guests" in guests
            else guests
        ),
        "zfs_pool": zfs.get("pool_status", zfs),
        "zfs_datasets": zfs.get("datasets", zfs),
        "host": host,
    }


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
    a, b = _flatten(old), _flatten(new)
    changed = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return {"changed": changed, "details": {k: {"was": a.get(k), "now": b.get(k)} for k in changed}}


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
        backlog = self._store.drain_pending()
        if not delta["changed"] and not backlog:
            return
        prompt = json.dumps(
            {"state": state, "change": delta, "backlog": backlog}, indent=2, sort_keys=True
        )
        try:
            answer = await self._agent.run(prompt, priority="daemon", system=SYSTEM_DAEMON)
        except Exception as exc:
            for item in [delta, *backlog]:
                self._store.queue_pending(item)
            if not self._degraded:
                self._degraded = True
                await self._notify(
                    "#homelab",
                    f":warning: agent degraded, model provider unreachable ({exc}). "
                    "Still watching, changes are queued.",
                )
            return
        if self._degraded:
            self._degraded = False
            await self._notify("#homelab", ":white_check_mark: agent recovered, backlog drained.")
        self._store.record_event("tick", {"change": delta, "answer": answer})
