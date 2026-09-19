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
