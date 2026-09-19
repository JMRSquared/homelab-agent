from agent.store import Store


def test_snapshot_round_trip(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.last_snapshot() is None
    s.put_snapshot({"guests": [{"id": 101, "status": "running"}]})
    assert s.last_snapshot() == {"guests": [{"id": 101, "status": "running"}]}


def test_pending_queue_drains_once(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.queue_pending({"changed": ["jellyfin"]})
    assert s.drain_pending() == [{"changed": ["jellyfin"]}]
    assert s.drain_pending() == []


def test_events_are_append_only(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    first = s.record_event("tool_call", {"tool": "guests_list"})
    second = s.record_event("tool_call", {"tool": "zfs_report"})
    assert second == first + 1
