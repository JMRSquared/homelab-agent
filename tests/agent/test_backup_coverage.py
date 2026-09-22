"""Off-site backup coverage.

The rule these tests exist to hold: a local ZFS snapshot is not a backup.
It lives on the same pool as the data and dies with it, so a dataset whose
only protection is snapshots is unprotected - and must be reported that
way, in those words, not softened into "has snapshots".
"""

import asyncio
import datetime as dt
import json

import pytest

from agent import backup_coverage, backup_policy, tick
from agent.collectors import backup as backup_collector
from agent.store import Store
from agent.tools import backup as backup_tool  # noqa: F401  (registers backup_coverage)
from agent.tools import base, infra

NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)

# The live pool, as of the brief: 692G of re-fetchable media, 73G of family
# photos that nothing reproduces, and 258G of Proxmox VM dumps that back up
# the guests, not the photos - all on the same pool.
LIVE_DATASETS = [
    {"name": "tank/media", "used": "692G", "avail": "980G", "snapshot_count": 0},
    {"name": "tank/immich", "used": "73G", "avail": "980G", "snapshot_count": 14},
    {"name": "tank/backups", "used": "258G", "avail": "980G", "snapshot_count": 2},
]


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BACKUP_STATE", str(tmp_path / "coverage.json"))
    monkeypatch.delenv("AGENT_BACKUP_POLICY", raising=False)


def test_snapshots_alone_are_reported_as_unprotected():
    report = backup_coverage.report(LIVE_DATASETS, now=NOW)
    immich = report["datasets"]["tank/immich"]

    assert immich["snapshot_count"] == 14
    assert immich["protection"] == "none"
    assert immich["snapshots_only"] is True
    assert "tank/immich" in report["unprotected_irreplaceable"]
    assert report["totals"]["irreplaceable_unprotected_gb"] == 73.0
    assert report["totals"]["protected_gb"] == 0.0
    assert "not coverage" in report["summary"]


def test_vm_dumps_on_the_same_pool_do_not_protect_the_photos():
    """tank/backups holds 258G of Proxmox dumps. That is a real thing to
    have, and it is not an off-site copy of anything."""
    report = backup_coverage.report(LIVE_DATASETS, now=NOW)
    assert report["datasets"]["tank/backups"]["protection"] == "none"
    assert report["unprotected_irreplaceable"] == ["tank/immich"]


def test_reproducible_data_is_not_counted_as_exposed():
    report = backup_coverage.report(LIVE_DATASETS, now=NOW)
    assert report["datasets"]["tank/media"]["classification"] == "reproducible"
    assert "tank/media" not in report["unprotected_irreplaceable"]


def test_an_unclassified_dataset_is_surfaced_rather_than_assumed_safe():
    datasets = [*LIVE_DATASETS, {"name": "tank/dev", "used": "40G", "snapshot_count": 1}]
    report = backup_coverage.report(datasets, now=NOW)
    assert report["unclassified"] == ["tank/dev"]
    assert report["datasets"]["tank/dev"]["classification"] == "unknown"


def test_a_declared_offsite_copy_counts_and_nothing_else_does(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "datasets": {"tank/immich": {"classification": "irreplaceable", "note": "photos"}},
                "offsite": {"tank/immich": {"target": "b2:homelab-photos", "kind": "restic"}},
            }
        )
    )
    monkeypatch.setenv("AGENT_BACKUP_POLICY", str(policy_path))
    report = backup_coverage.report(LIVE_DATASETS, now=NOW)
    immich = report["datasets"]["tank/immich"]
    assert immich["protection"] == "offsite"
    assert immich["offsite_target"] == "b2:homelab-photos"
    assert immich["snapshots_only"] is False
    assert report["unprotected_irreplaceable"] == []


def test_a_broken_policy_file_falls_back_to_the_default(tmp_path, monkeypatch):
    bad = tmp_path / "policy.json"
    bad.write_text("{not json")
    monkeypatch.setenv("AGENT_BACKUP_POLICY", str(bad))
    assert backup_policy.load_policy() == backup_policy.DEFAULT_POLICY


def test_protection_change_time_holds_steady_then_moves(tmp_path, monkeypatch):
    first = backup_coverage.report(LIVE_DATASETS, now=NOW)
    later = backup_coverage.report(LIVE_DATASETS, now=NOW + dt.timedelta(hours=6))
    assert later["protection_changed_at"]["tank/immich"] == first["protection_changed_at"][
        "tank/immich"
    ]

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "datasets": {"tank/immich": {"classification": "irreplaceable", "note": "photos"}},
                "offsite": {"tank/immich": {"target": "b2:homelab-photos"}},
            }
        )
    )
    monkeypatch.setenv("AGENT_BACKUP_POLICY", str(policy_path))
    protected = backup_coverage.report(LIVE_DATASETS, now=NOW + dt.timedelta(days=1))
    assert protected["protection_changed_at"]["tank/immich"] != first["protection_changed_at"][
        "tank/immich"
    ]


def test_an_unwritable_state_path_still_produces_a_report(monkeypatch):
    monkeypatch.setenv("AGENT_BACKUP_STATE", "/nonexistent-root/nope/coverage.json")
    report = backup_coverage.report(LIVE_DATASETS, now=NOW)
    assert report["unprotected_irreplaceable"] == ["tank/immich"]


def test_the_tool_answers_from_the_live_zfs_listing(monkeypatch):
    monkeypatch.setattr(
        infra, "zfs_report", lambda: {"pool_status": "ONLINE", "datasets": LIVE_DATASETS}
    )
    out = base.dispatch("backup_coverage", {})
    assert out["ok"] is True
    assert out["result"]["unprotected_irreplaceable"] == ["tank/immich"]
    assert out["result"]["datasets"]["tank/immich"]["snapshots_only"] is True


def test_byte_level_growth_does_not_wake_the_model_but_a_real_import_does(tmp_path, monkeypatch):
    """Unprotected data growing is the signal. Immich writing a few hundred
    megabytes an hour is not, and neither is the report's own timestamp -
    which changes on every single tick by construction."""
    sizes = ["73.00G", "73.02G", "73.05G", "73.09G", "73.12G"]
    listing = list(sizes)

    def zfs_report():
        used = listing.pop(0)
        return {
            "pool_status": "ONLINE",
            "datasets": [{"name": "tank/immich", "used": used, "snapshot_count": 14}],
        }

    monkeypatch.setattr(infra, "zfs_report", zfs_report)
    states = []
    for _ in sizes:
        from agent.collectors import zfs as zfs_collector

        zfs_collector._reset_cache()
        states.append(backup_collector.collect())

    monkeypatch.setattr(tick, "collect", lambda: states.pop(0))
    store = Store(str(tmp_path / "t.db"))

    class SilentAgent:
        runs = 0

        async def run(self, prompt, *, priority, system):
            type(self).runs += 1
            return "ok"

    async def _noop(channel, text):
        return None

    agent = SilentAgent()
    ticker = tick.Ticker(agent, store, notify=_noop)
    for _ in sizes:
        asyncio.run(ticker.once())
    assert agent.runs == 0

    # A real 40G import does register.
    grown = {"pool_status": "ONLINE",
             "datasets": [{"name": "tank/immich", "used": "113G", "snapshot_count": 14}]}
    from agent.collectors import zfs as zfs_collector

    monkeypatch.setattr(infra, "zfs_report", lambda: grown)
    zfs_collector._reset_cache()
    monkeypatch.setattr(tick, "collect", backup_collector.collect)
    asyncio.run(ticker.once())
    assert agent.runs == 1
