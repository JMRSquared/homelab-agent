import threading

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


def test_peek_pending_does_not_delete(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.queue_pending({"n": 1})
    s.queue_pending({"n": 2})
    assert s.pending_count() == 2
    peeked = s.peek_pending(10)
    assert [item["n"] for _, item in peeked] == [1, 2]
    # Peeking twice returns the same rows - nothing was consumed.
    assert s.pending_count() == 2
    assert [item["n"] for _, item in s.peek_pending(10)] == [1, 2]


def test_peek_pending_respects_limit(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    for i in range(5):
        s.queue_pending({"n": i})
    assert s.pending_count() == 5
    peeked = s.peek_pending(2)
    assert [item["n"] for _, item in peeked] == [0, 1]


def test_delete_pending_removes_only_named_rows(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.queue_pending({"n": 1})
    s.queue_pending({"n": 2})
    s.queue_pending({"n": 3})
    ids = [row_id for row_id, _ in s.peek_pending(10)]
    s.delete_pending(ids[:2])
    remaining = s.peek_pending(10)
    assert [item["n"] for _, item in remaining] == [3]


def test_concurrent_drain_pending_delivers_each_item_exactly_once(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    total = 300
    for i in range(total):
        s.queue_pending({"n": i})

    results: list[list[dict]] = []
    results_lock = threading.Lock()

    def worker() -> None:
        for _ in range(50):
            batch = s.drain_pending()
            if batch:
                with results_lock:
                    results.append(batch)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    delivered = [item["n"] for batch in results for item in batch]
    assert sorted(delivered) == list(range(total))
    assert s.drain_pending() == []
