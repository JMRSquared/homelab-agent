"""Token usage accounting for the model calls the two autonomous loops (and
Slack chat) make unattended.

`agent/model.py` records one row per API round-trip via
`Store.record_usage`, tagged with a `context` string the caller chose (see
`Agent.run`'s `context` parameter) - "daemon" for the 60s tick, "improve"
for the self-improvement cycle, "family" for Slack chat, or something more
specific a caller supplies. This module only aggregates what's already been
recorded; it never talks to the model or the provider's billing API itself.

Honesty about what this covers: these totals are the token counts MiniMax
handed back with each response, summed in our own database. They are not a
bill, they do not account for any plan-level rounding, caching, or pricing
tiers, and a request that failed before a response came back (a timeout, a
transport error) contributes nothing here even though it may have cost
something on the provider's side. The plan's own usage dashboard is the
authority on what was actually billed - this is instrumentation, not an
invoice. `NOTE` below is the exact sentence handed back with every summary
so nobody mistakes one for the other.
"""

import datetime as dt
from typing import Any

from agent.store import Store

NOTE = (
    "These totals are token counts the model API returned with each response, summed "
    "from our own event log - not a bill. They do not cover requests that failed "
    "before a response came back, and they carry no pricing or plan-tier information. "
    "The provider's own usage dashboard is the authority on what was actually billed; "
    "treat this as an internal instrument, not an invoice."
)


def _empty_bucket() -> dict[str, int]:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _add(bucket: dict[str, int], row: dict[str, Any]) -> None:
    bucket["requests"] += 1
    bucket["prompt_tokens"] += int(row["prompt_tokens"])
    bucket["completion_tokens"] += int(row["completion_tokens"])
    bucket["total_tokens"] += int(row["total_tokens"])


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty_bucket()
    by_context: dict[str, dict[str, int]] = {}
    by_model: dict[str, dict[str, int]] = {}
    for row in rows:
        _add(total, row)
        _add(by_context.setdefault(str(row["context"]), _empty_bucket()), row)
        _add(by_model.setdefault(str(row["model"]), _empty_bucket()), row)
    return {"total": total, "by_context": by_context, "by_model": by_model}


def summarize(store: Store, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """Today (since UTC midnight) and the trailing 7 days, each broken down
    by context and by model. `now` is injectable for tests; production
    callers leave it as the real current time.
    """
    now = now or dt.datetime.now(dt.UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - dt.timedelta(days=7)
    return {
        "generated_at": now.isoformat(),
        "today": _summarize(store.usage_since(today_start.isoformat())),
        "last_7_days": _summarize(store.usage_since(week_start.isoformat())),
        "note": NOTE,
    }
