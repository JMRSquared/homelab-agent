import datetime as dt
import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
  thread_ts TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  history TEXT NOT NULL
);
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def record_event(self, kind: str, payload: dict[str, Any]) -> int:
        cur = self._db.execute(
            "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
            (_now(), kind, json.dumps(payload)),
        )
        self._db.commit()
        return int(cur.lastrowid or 0)

    def put_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO snapshots (at, body) VALUES (?, ?)", (_now(), json.dumps(snapshot))
        )
        self._db.commit()

    def last_snapshot(self) -> dict[str, Any] | None:
        row = self._db.execute("SELECT body FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def queue_pending(self, diff: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO pending (at, body) VALUES (?, ?)", (_now(), json.dumps(diff))
        )
        self._db.commit()

    def drain_pending(self) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT id, body FROM pending ORDER BY id").fetchall()
        if not rows:
            return []
        self._db.execute("DELETE FROM pending WHERE id <= ?", (rows[-1][0],))
        self._db.commit()
        return [json.loads(body) for _, body in rows]
