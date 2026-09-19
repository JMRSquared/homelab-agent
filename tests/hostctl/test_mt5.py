import sqlite3
import time

import pytest

from hostctl import mt5

SCHEMA = """
CREATE TABLE equity(
    ts_epoch INTEGER PRIMARY KEY, ts_utc TEXT, equity REAL, balance REAL,
    floating REAL, open_positions INTEGER, terminal_trade_allowed INTEGER,
    ea_trade_allowed INTEGER, connected INTEGER, hb_utc TEXT,
    hb_age_s INTEGER, live INTEGER
);
"""


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "tazzie.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(mt5, "DB_PATH", str(path))
    return path


def _insert(path, **fields):
    conn = sqlite3.connect(path)
    cols = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO equity ({cols}) VALUES ({placeholders})", list(fields.values()))
    conn.commit()
    conn.close()


def test_status_reports_the_latest_row(db_path):
    now = int(time.time())
    _insert(
        db_path,
        ts_epoch=now,
        ts_utc="2026-09-19T08:20:02Z",
        equity=685.14,
        balance=700.0,
        live=1,
    )
    result = mt5.status()
    assert result["latest"]["equity"] == 685.14
    assert result["latest"]["balance"] == 700.0
    assert result["latest"]["age_s"] < 5


def test_status_distinguishes_latest_from_latest_with_open_positions(db_path):
    """Regression test for the live finding: the freshest row (OCR-based)
    often lacks open_positions, and the row that has it (the EA's file
    heartbeat) can be far staler. Both must be reported separately, not
    merged into one number."""
    stale = int(time.time()) - 361466  # matches the live host reading, ~100h stale
    fresh = int(time.time())
    _insert(
        db_path,
        ts_epoch=stale,
        ts_utc="2026-09-15T00:00:00Z",
        equity=704.75,
        open_positions=1,
        hb_age_s=361466,
        live=0,
    )
    _insert(
        db_path,
        ts_epoch=fresh,
        ts_utc="2026-09-19T08:20:02Z",
        equity=685.14,
        balance=700.0,
        live=1,
    )
    result = mt5.status()
    assert result["latest"]["ts_epoch"] == fresh
    assert result["latest"]["open_positions"] is None
    assert result["latest_with_open_positions"]["ts_epoch"] == stale
    assert result["latest_with_open_positions"]["open_positions"] == 1
    assert result["latest_with_open_positions"]["age_s"] > 360000


def test_status_handles_an_empty_table(db_path):
    result = mt5.status()
    assert result["latest"] is None
    assert result["latest_with_open_positions"] is None
