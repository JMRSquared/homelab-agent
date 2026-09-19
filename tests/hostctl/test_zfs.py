import datetime as dt

import pytest

from hostctl import zfs


def test_snapshot_returns_full_name(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(zfs, "_run", lambda argv: seen.append(argv) or "")
    name = zfs.snapshot("tank/immich", "pre-upgrade")
    assert name.startswith("tank/immich@pre-upgrade-")
    # The rate-limit check runs first (a `zfs list -t snapshot` read), so the
    # actual `zfs snapshot` create call is the last one issued, not the first.
    assert seen[-1][:2] == ["zfs", "snapshot"]


def test_no_destroy_verb_exists():
    assert not [n for n in dir(zfs) if "destroy" in n.lower()]


def test_snapshot_rejects_dataset_outside_tank(monkeypatch):
    monkeypatch.setattr(zfs, "_run", lambda argv: "")
    with pytest.raises(ValueError):
        zfs.snapshot("rpool/other", "pre-upgrade")


def test_snapshot_of_pool_root_is_allowed(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(zfs, "_run", lambda argv: seen.append(argv) or "")
    name = zfs.snapshot("tank", "pre-upgrade")
    assert name.startswith("tank@pre-upgrade-")


def test_snapshot_rejects_a_repeat_within_the_rate_limit_window(monkeypatch):
    recent = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    listing = f"tank/immich@pre-upgrade-20260101T000000Z\t{int(recent.timestamp())}\n"

    def fake_run(argv: list[str]) -> str:
        if argv[:3] == ["zfs", "list", "-H"] and "snapshot" in argv:
            return listing
        raise AssertionError(f"unexpected argv in rate-limited path: {argv}")

    monkeypatch.setattr(zfs, "_run", fake_run)

    with pytest.raises(zfs.SnapshotRateLimitError, match="pre-upgrade"):
        zfs.snapshot("tank/immich", "pre-upgrade")


def test_snapshot_allowed_again_after_the_rate_limit_window(monkeypatch):
    old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=7)
    listing = f"tank/immich@pre-upgrade-20260101T000000Z\t{int(old.timestamp())}\n"
    seen: list[list[str]] = []

    def fake_run(argv: list[str]) -> str:
        seen.append(argv)
        if argv[0] == "zfs" and argv[1] == "list" and "snapshot" in argv:
            return listing
        return ""

    monkeypatch.setattr(zfs, "_run", fake_run)

    name = zfs.snapshot("tank/immich", "pre-upgrade")
    assert name.startswith("tank/immich@pre-upgrade-")


def test_status_reports_snapshot_count_and_usage(monkeypatch):
    def fake_run(argv: list[str]) -> str:
        joined = " ".join(argv)
        if argv[:2] == ["zpool", "status"]:
            return "pool: tank\n state: ONLINE\n"
        if "usedbysnapshots" in joined:
            return "tank/immich\t2147483648\n"
        if argv[-2:] == ["-t", "snapshot"]:
            return "tank/immich@a-1\ntank/immich@a-2\n"
        if argv[:2] == ["zfs", "list"]:
            return "tank/immich\t10G\t5G\t10G\n"
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(zfs, "_run", fake_run)

    result = zfs.status()
    dataset = result["datasets"][0]  # type: ignore[index]
    assert dataset["snapshot_count"] == 2  # type: ignore[call-overload]
    assert dataset["usedbysnapshots_gb"] == 2.0  # type: ignore[call-overload]
