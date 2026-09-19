from hostctl import zfs


def test_snapshot_returns_full_name(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(zfs, "_run", lambda argv: seen.append(argv) or "")
    name = zfs.snapshot("tank/immich", "pre-upgrade")
    assert name.startswith("tank/immich@pre-upgrade-")
    assert seen[0][:2] == ["zfs", "snapshot"]


def test_no_destroy_verb_exists():
    assert not [n for n in dir(zfs) if "destroy" in n.lower()]
