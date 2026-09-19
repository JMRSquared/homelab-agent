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


def test_collect_async_does_not_block_the_event_loop(monkeypatch):
    """collect() makes three blocking httpx calls; run inline inside
    Ticker.once's coroutine (the pre-fix shape), a hung hostctl stalls the
    whole event loop for as long as it blocks - the Slack websocket and
    every family reply along with it. collect_async() must run it via
    asyncio.to_thread, the same treatment I1 gave dispatch()."""
    import time

    from agent.tools import infra

    def slow_guests() -> dict:
        time.sleep(0.3)
        return {"guests": []}

    monkeypatch.setattr(infra, "guests_list", slow_guests)
    monkeypatch.setattr(infra, "zfs_report", lambda: {"pool_status": "", "datasets": []})
    monkeypatch.setattr(infra, "host_metrics", lambda: {})

    events: list[tuple[str, float]] = []

    async def other_task() -> None:
        await asyncio.sleep(0.05)
        events.append(("other_task_done", time.monotonic()))

    async def scenario() -> tuple[float, float]:
        start = time.monotonic()
        await asyncio.gather(tick.collect_async(), other_task())
        return start, time.monotonic()

    start, end = asyncio.run(scenario())

    # collect()'s blocking sleep is 0.3s; other_task's is 0.05s. If
    # collect_async() blocked the loop, other_task couldn't run until
    # collect() finished, so it would land near `end` (~0.3s after start).
    # Off the loop, it lands near its own 0.05s regardless of collect()'s
    # much longer block.
    other_done_at = events[0][1] - start
    assert other_done_at < 0.2, f"other_task was starved: finished at {other_done_at:.3f}s"
    assert end - start >= 0.3  # sanity: collect() really did take its full 0.3s


SAMPLE_ZPOOL_STATUS_SCRUBBING = """\
  pool: tank
 state: ONLINE
  scan: scrub in progress since Fri Sep 18 03:00:01 2026
        1.23T scanned at 105M/s, 45.62% done, 0 days 02:15:33 to go
        0B repaired, 0.00% done
config:

    NAME        STATE     READ WRITE CKSUM
    tank        ONLINE       0     0     0
      raidz1-0  ONLINE       0     0     0
        sda     ONLINE       0     0     0
        sdb     ONLINE       0     0     0

errors: No known data errors
"""

SAMPLE_ZPOOL_STATUS_SCRUBBING_LATER = """\
  pool: tank
 state: ONLINE
  scan: scrub in progress since Fri Sep 18 03:00:01 2026
        1.30T scanned at 106M/s, 48.10% done, 0 days 02:05:11 to go
        0B repaired, 0.00% done
config:

    NAME        STATE     READ WRITE CKSUM
    tank        ONLINE       0     0     0
      raidz1-0  ONLINE       0     0     0
        sda     ONLINE       0     0     0
        sdb     ONLINE       0     0     0

errors: No known data errors
"""

SAMPLE_ZPOOL_STATUS_DEGRADED = """\
  pool: tank
 state: DEGRADED
  scan: scrub in progress since Fri Sep 18 03:00:01 2026
        1.30T scanned at 106M/s, 48.10% done, 0 days 02:05:11 to go
        0B repaired, 0.00% done
config:

    NAME        STATE     READ WRITE CKSUM
    tank        DEGRADED     0     0     0
      raidz1-0  DEGRADED     0     0     0
        sda     ONLINE       0     0     0
        sdb     UNAVAIL      0     0     0

errors: No known data errors
"""


def _realistic_state(*, pool_status: str, used_gb: float, avail_gb: float) -> dict:
    return {
        "guests": {"101": "running"},
        "zfs_pool": pool_status,
        "zfs_datasets": [
            {
                "name": "tank/immich",
                "used": f"{used_gb:.2f}G",
                "avail": f"{avail_gb:.2f}G",
                "refer": f"{used_gb:.2f}G",
                "usedbysnapshots_gb": 2.34,
                "snapshot_count": 3,
            }
        ],
        "host": {
            "load1": 0.42,
            "mem_total_gb": 32.0,
            "mem_used_gb": 10.1,
            "arc_gb": 8.0,
            "uptime_s": 86400.0,
        },
    }


def test_every_host_leaf_key_is_accounted_for_in_the_diff_policy():
    """Regression test for B1-B3: the first fix wave passed while zfs_pool,
    used/avail/refer, and band hysteresis all still leaked real per-tick
    noise straight into the diff. This enumerates every leaf key a realistic
    `host_metrics()` sample can produce and asserts each one is in exactly
    one of tick.py's own policy sets - not "whatever collect() happens to
    return" but a decision, made explicit, that a newly-added field must
    also get before this test passes again."""
    sample_host_keys = set(_realistic_state(pool_status="x", used_gb=1, avail_gb=1)["host"])
    accounted_for = tick.HOST_EXCLUDED | tick.HOST_BANDED | tick.HOST_PASSTHROUGH
    assert sample_host_keys == accounted_for, (
        f"host keys not accounted for in tick.py's diff policy: "
        f"{sample_host_keys - accounted_for}"
    )
    # HOST_BANDED must actually collapse realistic per-tick drift, not just
    # be listed - band every key and confirm two nearby raw readings project
    # to the same value.
    drifted = {"load1": 0.51, "mem_used_gb": 10.4, "arc_gb": 8.05}
    base = {"load1": 0.42, "mem_used_gb": 10.1, "arc_gb": 8.0}
    for key in tick.HOST_BANDED:
        step = tick._HOST_BAND_STEP[key]
        first = tick._band_with_deadband(base[key], None, step)
        second = tick._band_with_deadband(drifted[key], first, step)
        assert first == second, f"{key} did not absorb realistic drift"


def test_every_dataset_leaf_key_is_accounted_for_in_the_diff_policy():
    sample = _realistic_state(pool_status="x", used_gb=500.0, avail_gb=100.0)
    sample_dataset_keys = set(sample["zfs_datasets"][0])
    accounted_for = tick.DATASET_EXCLUDED | tick.DATASET_BANDED | tick.DATASET_PASSTHROUGH
    assert sample_dataset_keys == accounted_for, (
        f"dataset keys not accounted for in tick.py's diff policy: "
        f"{sample_dataset_keys - accounted_for}"
    )


def test_zfs_pool_projection_only_keeps_state_errors_and_normalized_scan():
    projected = tick._project_zfs_pool(SAMPLE_ZPOOL_STATUS_SCRUBBING)
    assert set(projected) <= tick.ZFS_POOL_KEPT_KEYS
    assert "state" in projected and "scan" in projected and "errors" in projected


def test_diff_ignores_scrub_progress_but_catches_pool_degradation():
    """B1: a scrub's scanned-bytes/percent/ETA text moves every second for
    hours (Debian's zfsutils-linux ships a monthly scrub cron; the pool's
    last scrub ran over three hours). Two collections differing only in
    scrub progress must produce no change; a pool going ONLINE -> DEGRADED
    must still be caught."""
    old = _realistic_state(
        pool_status=SAMPLE_ZPOOL_STATUS_SCRUBBING, used_gb=500.0, avail_gb=100.0
    )
    new = _realistic_state(
        pool_status=SAMPLE_ZPOOL_STATUS_SCRUBBING_LATER, used_gb=500.0, avail_gb=100.0
    )
    assert tick.diff(old, new)["changed"] == []

    degraded = _realistic_state(
        pool_status=SAMPLE_ZPOOL_STATUS_DEGRADED, used_gb=500.0, avail_gb=100.0
    )
    assert tick.diff(old, degraded)["changed"] == ["zfs_pool.state"]


def test_diff_ignores_pool_wide_avail_drift_from_unrelated_writes():
    """B2: `avail` is pool-wide, so one GB written anywhere on the pool (a
    Jellyfin/debrid cache write, an Immich ingest) changes it - and
    therefore every dataset row that reports it - on every tick."""
    old = _realistic_state(pool_status="x", used_gb=500.0, avail_gb=100.20)
    new = _realistic_state(pool_status="x", used_gb=500.0, avail_gb=99.85)
    assert tick.diff(old, new)["changed"] == []


def test_diff_still_catches_a_real_capacity_move():
    old = _realistic_state(pool_status="x", used_gb=500.0, avail_gb=100.0)
    new = _realistic_state(pool_status="x", used_gb=505.0, avail_gb=95.0)
    changed = tick.diff(old, new)["changed"]
    assert changed, "a real multi-GB capacity move should register as a change"
    assert all(c.startswith("zfs_datasets") for c in changed)


def test_band_with_deadband_has_hysteresis_at_the_edge():
    """B3: naive per-call rounding has no memory, so a value sitting near a
    band edge can cross it every tick on ordinary noise. Comparing against
    the previously *reported* value with a full-step deadband (not half -
    see the round-2 follow-up) absorbs drift that would otherwise flip the
    band back and forth, including drift that lands right on a boundary."""
    step = 1.0
    reported = tick._band_with_deadband(8.0, None, step)
    assert reported == 8.0
    # Realistic drift, including values that cross the nearby 8.5 boundary,
    # must not move the reported value - a half-step deadband would have
    # flipped on the 8.6/8.55-shaped readings here.
    for raw in (8.03, 7.97, 8.05, 7.95, 8.6, 8.9, 7.1):
        reported = tick._band_with_deadband(raw, reported, step)
        assert reported == 8.0
    # A real move - more than a full step away from what was last reported -
    # does clear the deadband and get reported.
    reported = tick._band_with_deadband(9.1, reported, step)
    assert reported == 9.0


def test_load1_oscillating_across_a_band_edge_never_wakes_across_consecutive_ticks(
    tmp_path, monkeypatch
):
    """Regression test for the round-2 follow-up: a real load1 sample from
    the production host, 20s apart, parked on the 0.5/1.0 band edge -
    0.69 -> 0.5, 0.76 -> 1.0, 0.65 -> 0.5 under plain per-tick rounding,
    and still under a half-step deadband (0.263 and 0.35 both clear a 0.25
    threshold). This must mirror Ticker.once's actual call pattern -
    previous snapshot against current, in sequence - not a first-vs-last
    comparison: 1->2 and 2->3 each wake the model under the bug even though
    1->3 alone is quiet, which is exactly what let this survive a green
    suite twice before."""
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    loads = [0.69091796875, 0.7626953125, 0.650390625, 0.78, 0.71]
    states = [
        _collected("running", load1=v, mem_used_gb=10.0, uptime_s=100.0 + i * 20)
        for i, v in enumerate(loads)
    ]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(agent, store, notify=_noop)

    for _ in loads:
        asyncio.run(ticker.once())

    assert agent.runs == 0


def test_load1_sustained_climb_reports_once_then_settles(tmp_path, monkeypatch):
    """A genuine sustained move - not noise - must still be caught, and
    exactly once: it reports on the tick it happens, then goes quiet again
    once it's the new steady state."""
    from agent.store import Store

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    loads = [0.5, 0.5, 3.0, 3.0, 3.0]
    states = [
        _collected("running", load1=v, mem_used_gb=10.0, uptime_s=100.0 + i * 20)
        for i, v in enumerate(loads)
    ]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    ticker = tick.Ticker(agent, store, notify=_noop)

    runs_after_each_tick = []
    for _ in loads:
        asyncio.run(ticker.once())
        runs_after_each_tick.append(agent.runs)

    # tick1: baseline, no comparison yet. tick2: 0.5->0.5, quiet. tick3:
    # 0.5->3.0, a real move, wakes the model once. tick4/5: steady at 3.0,
    # quiet again.
    assert runs_after_each_tick == [0, 0, 1, 1, 1]


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
