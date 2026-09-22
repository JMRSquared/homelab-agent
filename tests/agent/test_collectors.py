"""The registry, and the two collectors added on top of it.

The load-bearing test here is the first one: a full sweep of realistic,
consecutive readings on an idle homelab - heartbeat ages bouncing around,
a certificate counting down by fractions of a day, dataset sizes drifting,
load oscillating across a band edge - must wake the model zero times. It
drives `Ticker.once` in a loop rather than comparing the first reading to
the last, because the tick compares *consecutive* snapshots: an
endpoints-only test passed twice while the original wake storm was fully
live.
"""

import asyncio

import httpx
import pytest

from agent import clients, tick
from agent.collectors import certs, mt5, registry
from agent.store import Store
from agent.tools import infra


class SilentAgent:
    def __init__(self) -> None:
        self.runs = 0
        self.prompts: list[str] = []

    async def run(self, prompt, *, priority, system):
        self.runs += 1
        self.prompts.append(prompt)
        return "ok"


async def _noop(channel: str, text: str) -> None:
    return None


# One realistic minute of an idle homelab, ten times over. Every number
# that moves here moves the way it really does on the live host.
HEARTBEAT_AGES = [12, 47, 9, 55, 31, 7, 58, 22, 44, 3]
LOADS = [0.69, 0.76, 0.65, 0.78, 0.71, 0.69, 0.74, 0.66, 0.80, 0.70]
TICKS = len(LOADS)


@pytest.fixture
def idle_homelab(monkeypatch, tmp_path):
    """Patch every collector's source with an idle-but-drifting homelab."""
    monkeypatch.setenv("AGENT_BACKUP_STATE", str(tmp_path / "coverage.json"))
    clock = {"i": 0}

    def advance() -> int:
        i = clock["i"]
        clock["i"] += 1
        return i

    monkeypatch.setattr(
        infra, "guests_list", lambda: {"guests": [{"id": 101, "status": "running"}]}
    )
    monkeypatch.setattr(
        infra,
        "host_metrics",
        lambda: {
            "load1": LOADS[clock["i"] % TICKS],
            "mem_total_gb": 32.0,
            "mem_used_gb": 10.1 + 0.02 * (clock["i"] % TICKS),
            "arc_gb": 8.0,
            "uptime_s": 86400.0 + 60.0 * clock["i"],
        },
    )
    monkeypatch.setattr(
        infra,
        "zfs_report",
        lambda: {
            "pool_status": " state: ONLINE\n  scan: scrub repaired 0B in 03:11:02\nerrors: None\n",
            "datasets": [
                {
                    "name": "tank/immich",
                    "used": f"{73.0 + 0.01 * (clock['i'] % TICKS):.2f}G",
                    "avail": f"{980.0 - 0.02 * (clock['i'] % TICKS):.2f}G",
                    "refer": "73.0G",
                    "usedbysnapshots_gb": 2.34 + 0.001 * clock["i"],
                    "snapshot_count": 3,
                }
            ],
        },
    )

    def mt5_status():
        i = advance()
        return {
            "latest": {"ts_epoch": 1_000_000 + 60 * i, "equity": 685.14 + i, "age_s": 4 + i % 3},
            "latest_with_open_positions": {
                "ts_epoch": 1_000_000 + 60 * i,
                "age_s": HEARTBEAT_AGES[i % TICKS],
                "hb_age_s": 11,
                "open_positions": 2,
                "connected": 1,
                "terminal_trade_allowed": 1,
                "ea_trade_allowed": 1,
            },
        }

    monkeypatch.setattr(infra, "mt5_status", mt5_status)
    monkeypatch.setattr(
        clients,
        "hostctl_get_optional",
        lambda path, **kw: {
            "certs": [
                {
                    "name": "mail",
                    "ok": True,
                    "subject": "CN=mx.homelab.local",
                    "issuer": "CN=homelab-ca",
                    "not_after": "2026-11-21T09:00:00+00:00",
                    # A countdown, ticking down by a minute's worth each tick.
                    "days_remaining": 60.4137 - 0.000694 * clock["i"],
                }
            ]
        },
    )
    return clock


def test_idle_homelab_never_wakes_the_model_across_consecutive_ticks(idle_homelab, tmp_path):
    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    ticker = tick.Ticker(agent, store, notify=_noop)

    for _ in range(TICKS):
        asyncio.run(ticker.once())

    assert agent.runs == 0, f"woke the model on an idle homelab: {agent.prompts}"


def test_the_volatile_fields_really_are_present_in_the_state(idle_homelab):
    """Guard against the previous test passing because the new collectors
    quietly produced nothing at all."""
    state = tick.collect()
    assert state["mt5"]["heartbeat_age_s"] is not None
    assert state["certs"]["mail"]["days_remaining"] is not None
    assert state["backup"]["totals"]["irreplaceable_gb"] > 0


def test_every_mt5_leaf_key_is_accounted_for_in_the_diff_policy(idle_homelab, monkeypatch):
    """Both shapes this collector can emit - healthy, and degraded when the
    export can't be read - together account for exactly the policy's keys.
    A field added to either shape and to neither policy set fails here,
    before it can reach the live diff unbanded."""
    healthy = set(tick.collect()["mt5"])

    def boom():
        raise RuntimeError("gone")

    monkeypatch.setattr(infra, "mt5_status", boom)
    degraded = set(mt5.collect()["mt5"])

    assert healthy | degraded == mt5.POLICY.accounted_for, (
        f"mt5 keys not accounted for in the diff policy: "
        f"{(healthy | degraded) - mt5.POLICY.accounted_for}"
    )


def test_every_cert_leaf_key_is_accounted_for_in_the_diff_policy(idle_homelab):
    sample = set(tick.collect()["certs"]["mail"])
    assert sample == certs.POLICY.accounted_for


def test_a_heartbeat_going_stale_does_wake_the_model_once_per_threshold(tmp_path, monkeypatch):
    """The flip side: banding must not make the collector blind. A
    heartbeat that genuinely stops crosses one bucket boundary at a time,
    and each crossing is worth exactly one wake."""
    monkeypatch.setenv("AGENT_BACKUP_STATE", str(tmp_path / "coverage.json"))
    ages = [30, 90, 240, 420, 600, 900, 2400, 3600]
    seen = {"i": 0}

    def mt5_status():
        age = ages[seen["i"]]
        seen["i"] += 1
        return {
            "latest": {"age_s": 3},
            "latest_with_open_positions": {"age_s": age, "hb_age_s": 0, "open_positions": 1},
        }

    monkeypatch.setattr(infra, "mt5_status", mt5_status)
    states = [mt5.collect() for _ in ages]
    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))

    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    ticker = tick.Ticker(agent, store, notify=_noop)
    for _ in ages:
        asyncio.run(ticker.once())

    # fresh -> stale_gt_5m (at 420s) -> stale_gt_30m (at 2400s): two real
    # crossings out of eight ticks, not eight wakes.
    assert agent.runs == 2


def test_mt5_degrades_to_a_stable_unavailable_shape(monkeypatch):
    def boom():
        raise RuntimeError("no such file: tazzie.db")

    monkeypatch.setattr(infra, "mt5_status", boom)
    state = mt5.collect()
    assert state["mt5"]["available"] is False
    assert "tazzie.db" in state["mt5"]["reason"]
    # The reason is diagnostic only - it must never reach the diff, or a
    # varying error string would wake the model every minute.
    projected = mt5.COLLECTOR.projectors["mt5"](state["mt5"], None)
    assert "reason" not in projected


def test_a_missing_cert_route_degrades_to_nothing(monkeypatch):
    """The hostctl cert route is being built separately. Until it exists,
    the collector must produce nothing at all - not an error key, which
    would wake the model, and certainly not an exception."""
    monkeypatch.setenv("HOSTCTL_URL", "http://hostctl.test")
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")

    import respx

    with respx.mock:
        respx.get("http://hostctl.test/certs/status").mock(return_value=httpx.Response(404))
        assert certs.collect() == {}


def test_an_unreachable_hostctl_degrades_the_cert_collector_to_nothing(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", "http://hostctl.test")
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")

    import respx

    with respx.mock:
        respx.get("http://hostctl.test/certs/status").mock(
            side_effect=httpx.ConnectError("refused")
        )
        assert certs.collect() == {}


def test_a_missing_cert_route_never_wakes_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(clients, "hostctl_get_optional", lambda path, **kw: None)
    states = [certs.collect() for _ in range(5)]
    assert states == [{}, {}, {}, {}, {}]

    monkeypatch.setattr(tick, "collect", lambda: {**certs.collect(), "guests": {"101": "running"}})
    store = Store(str(tmp_path / "t.db"))
    agent = SilentAgent()
    ticker = tick.Ticker(agent, store, notify=_noop)
    for _ in range(5):
        asyncio.run(ticker.once())
    assert agent.runs == 0


def test_a_cert_crossing_a_threshold_wakes_the_model(monkeypatch):
    days = [31.2, 29.8]
    monkeypatch.setattr(
        clients,
        "hostctl_get_optional",
        lambda path, **kw: {
            "certs": [
                {"name": "mail", "ok": True, "not_after": "2026-11-21T09:00:00+00:00",
                 "days_remaining": days.pop(0)}
            ]
        },
    )
    before = certs.collect()
    after = certs.collect()
    assert tick.diff(before, after)["changed"] == ["certs.mail.expiry"]


def test_no_configured_cert_targets_produces_nothing(monkeypatch):
    """hostctl reports an empty list when nothing is configured to check.
    That is not a finding, and must not become a state key."""
    monkeypatch.setattr(clients, "hostctl_get_optional", lambda path, **kw: {"certs": []})
    assert certs.collect() == {}


def test_an_unreachable_target_is_reported_without_its_error_text(monkeypatch):
    """A target hostctl could not reach is worth knowing about once. The
    error text is not: it varies between refused/timeout/handshake wording
    for the same outage, so it stays out of the diff."""
    monkeypatch.setattr(
        clients,
        "hostctl_get_optional",
        lambda path, **kw: {
            "certs": [{"name": "mail", "ok": False, "error": "timed out"}]
        },
    )
    state = certs.collect()
    assert state["certs"]["mail"]["ok"] is False
    assert state["certs"]["mail"]["expiry"] == "unknown"
    assert state["certs"]["mail"]["unreachable_reason"] == "timed out"
    projected = certs.COLLECTOR.projectors["certs"](state["certs"], None)
    assert "unreachable_reason" not in projected["mail"]


def test_a_collector_that_raises_does_not_take_the_sweep_down():
    def broken() -> dict:
        raise RuntimeError("bug in the collector itself")

    collector = registry.Collector(name="broken", keys=("broken",), collect=broken)
    registry.register(collector)
    try:
        state = registry.collect_all()
    finally:
        registry._REGISTRY.pop("broken")
    assert "bug in the collector itself" in state["broken"]["error"]
    # and the other collectors still produced their keys
    assert "guests" in state
