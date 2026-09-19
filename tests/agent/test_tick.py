import asyncio

from agent import tick


def test_diff_detects_status_change():
    old = {"guests": {"101": "running"}, "zfs": {"state": "ONLINE"}}
    new = {"guests": {"101": "stopped"}, "zfs": {"state": "ONLINE"}}
    assert tick.diff(old, new)["changed"] == ["guests.101"]


def test_diff_of_identical_state_is_empty():
    state = {"guests": {"101": "running"}}
    assert tick.diff(state, state)["changed"] == []


def test_first_run_is_not_reported_as_change():
    assert tick.diff(None, {"guests": {"101": "running"}})["changed"] == []


def _collected(guests_status: str, load1: float, mem_used_gb: float, uptime_s: float) -> dict:
    return {
        "guests": {"101": guests_status},
        "zfs_pool": "ONLINE",
        "zfs_datasets": [
            {"name": "tank/immich", "used": "10G", "usedbysnapshots_gb": 3.201, "snapshot_count": 2}
        ],
        "host": {
            "load1": load1,
            "mem_total_gb": 32.0,
            "mem_used_gb": mem_used_gb,
            "arc_gb": 8.0,
            "uptime_s": uptime_s,
        },
    }


def test_diff_ignores_realistic_volatile_drift():
    """Regression test for C1: uptime, load, and memory always move a little
    from tick to tick even when nothing meaningful changed. Real collect()
    output diffed against itself with realistic drift must report no change."""
    old = _collected("running", load1=0.42, mem_used_gb=10.10, uptime_s=86400.0)
    new = _collected("running", load1=0.51, mem_used_gb=10.40, uptime_s=86460.0)
    assert tick.diff(old, new)["changed"] == []


def test_diff_still_detects_a_real_guest_change_amid_volatile_drift():
    old = _collected("running", load1=0.42, mem_used_gb=10.10, uptime_s=86400.0)
    new = _collected("stopped", load1=0.51, mem_used_gb=10.40, uptime_s=86460.0)
    assert tick.diff(old, new)["changed"] == ["guests.101"]


def test_diff_still_detects_a_real_load_spike():
    old = _collected("running", load1=0.3, mem_used_gb=10.0, uptime_s=100.0)
    new = _collected("running", load1=4.8, mem_used_gb=10.0, uptime_s=160.0)
    assert tick.diff(old, new)["changed"] == ["host.load1"]


class SilentAgent:
    def __init__(self):
        self.runs = 0

    async def run(self, prompt, *, priority, system):
        self.runs += 1
        return "ok"


def test_idle_tick_makes_no_model_call(tmp_path, monkeypatch):
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    monkeypatch.setattr(tick, "collect", lambda: {"guests": {"101": "running"}})
    ticker = tick.Ticker(agent, store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert agent.runs == 0


def test_changed_tick_calls_the_model(tmp_path, monkeypatch):
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    states = [{"guests": {"101": "running"}}, {"guests": {"101": "stopped"}}]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(agent, store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert agent.runs == 1


def test_model_failure_queues_the_diff(tmp_path, monkeypatch):
    from agent.store import Store

    class Broken:
        async def run(self, prompt, *, priority, system):
            raise RuntimeError("provider down")

    store = Store(str(tmp_path / "t.db"))
    states = [{"guests": {"101": "running"}}, {"guests": {"101": "stopped"}}]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(Broken(), store, notify=_noop)
    asyncio.run(ticker.once())
    asyncio.run(ticker.once())
    assert store.drain_pending() != []


def test_degraded_alert_fires_once(tmp_path, monkeypatch):
    from agent.store import Store

    class Broken:
        async def run(self, prompt, *, priority, system):
            raise RuntimeError("provider down")

    store = Store(str(tmp_path / "t.db"))
    states = [
        {"guests": {"101": "running"}},
        {"guests": {"101": "stopped"}},
        {"guests": {"101": "running"}},
    ]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    notified: list[tuple[str, str]] = []

    async def record(channel: str, text: str) -> None:
        notified.append((channel, text))

    ticker = tick.Ticker(Broken(), store, notify=record)
    asyncio.run(ticker.once())  # first run, establishes baseline, no change
    asyncio.run(ticker.once())  # change detected, model fails -> degraded alert #1
    asyncio.run(ticker.once())  # change persists, model still fails -> no repeat alert
    assert len(notified) == 1
    assert "degraded" in notified[0][1]


def test_backlog_accumulates_oldest_first_across_outage(tmp_path, monkeypatch):
    from agent.store import Store

    class Broken:
        async def run(self, prompt, *, priority, system):
            raise RuntimeError("provider down")

    store = Store(str(tmp_path / "t.db"))
    states = [
        {"guests": {"101": "running"}},
        {"guests": {"101": "stopped"}},
        {"guests": {"101": "running"}},
    ]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(Broken(), store, notify=_noop)
    asyncio.run(ticker.once())  # first run, establishes baseline, no change
    asyncio.run(ticker.once())  # 101 running -> stopped, model fails, queued
    asyncio.run(ticker.once())  # 101 stopped -> running, model fails, queued

    backlog = store.drain_pending()
    assert len(backlog) == 2
    assert backlog[0]["details"]["guests.101"] == {"was": '"running"', "now": '"stopped"'}
    assert backlog[1]["details"]["guests.101"] == {"was": '"stopped"', "now": '"running"'}
    # drain_pending() empties the table, so a second drain proves nothing lost.
    assert store.drain_pending() == []


def test_backlog_survives_across_recovery_and_is_deleted_only_on_success(tmp_path, monkeypatch):
    """Regression test for the delete-before-call ordering bug: a queued
    backlog must stay queued until a model call that actually consumed it
    succeeds, not be deleted up front and hopefully re-queued on failure."""
    from agent.store import Store

    class Broken:
        async def run(self, prompt, *, priority, system):
            raise RuntimeError("provider down")

    class Working:
        async def run(self, prompt, *, priority, system):
            return "handled"

    store = Store(str(tmp_path / "t.db"))
    states = [
        {"guests": {"101": "running"}},
        {"guests": {"101": "stopped"}},
    ]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(Broken(), store, notify=_noop)
    asyncio.run(ticker.once())  # baseline
    asyncio.run(ticker.once())  # change, model fails, delta queued

    assert store.pending_count() == 1

    # Simulate a restart: a fresh Ticker over the same store, model now works.
    monkeypatch.setattr(tick, "collect", lambda: {"guests": {"101": "stopped"}})
    ticker2 = tick.Ticker(Working(), store, notify=_noop)
    asyncio.run(ticker2.once())

    assert store.pending_count() == 0


def test_backlog_summary_coalesces_rather_than_replaying(tmp_path, monkeypatch):
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    for i in range(5):
        detail = {"guests.101": {"was": str(i), "now": str(i + 1)}}
        store.queue_pending({"changed": ["guests.101"], "details": detail})
    backlog = store.peek_pending(3)
    summary = tick._summarize_backlog([item for _, item in backlog], store.pending_count())
    assert summary["queued_diffs"] == 3
    assert summary["total_pending"] == 5
    assert summary["omitted_older_diffs"] == 2
    assert summary["changed_keys"]["guests.101"]["times_changed"] == 3


def test_collect_survives_partial_hostctl_failure(monkeypatch):
    from agent.tools import infra

    def boom() -> dict:
        raise RuntimeError("hostctl unreachable")

    monkeypatch.setattr(infra, "guests_list", boom)
    monkeypatch.setattr(infra, "zfs_report", lambda: {"pool_status": "ONLINE", "datasets": {}})
    monkeypatch.setattr(infra, "host_metrics", lambda: {"load": 0.1})

    state = tick.collect()
    assert "error" in state["guests"]
    assert state["zfs_pool"] == "ONLINE"
    assert state["host"] == {"load": 0.1}


async def _noop(channel: str, text: str) -> None:
    return None
