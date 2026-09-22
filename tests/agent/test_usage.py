import datetime as dt

from agent import usage
from agent.store import Store


def test_summarize_buckets_today_and_week_by_context_and_model(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    now = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)

    store.record_usage(
        context="daemon", model="MiniMax-M3", prompt_tokens=100, completion_tokens=20,
        total_tokens=120,
    )
    store.record_usage(
        context="family", model="MiniMax-M3", prompt_tokens=50, completion_tokens=10,
        total_tokens=60,
    )

    result = usage.summarize(store, now=now)

    assert result["today"]["total"] == {
        "requests": 2, "prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180,
    }
    assert result["today"]["by_context"]["daemon"]["total_tokens"] == 120
    assert result["today"]["by_context"]["family"]["total_tokens"] == 60
    assert result["today"]["by_model"]["MiniMax-M3"]["requests"] == 2
    assert "not a bill" in result["note"]


def test_summarize_excludes_rows_outside_the_window(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    old = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    now = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)

    # Insert a usage row directly with an old timestamp, bypassing
    # record_usage's "now" - the only way to get a row outside the window.
    with store._lock:
        store._db.execute(
            "INSERT INTO usage (at, context, model, prompt_tokens, completion_tokens, "
            "total_tokens) VALUES (?, ?, ?, ?, ?, ?)",
            (old.isoformat(), "daemon", "MiniMax-M3", 10, 10, 20),
        )
        store._db.commit()

    result = usage.summarize(store, now=now)
    assert result["today"]["total"]["requests"] == 0
    assert result["last_7_days"]["total"]["requests"] == 0


def test_empty_store_summarizes_to_zero(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    result = usage.summarize(store)
    assert result["today"]["total"]["requests"] == 0
    assert result["today"]["by_context"] == {}
