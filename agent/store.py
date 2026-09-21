import datetime as dt
import json
import sqlite3
import threading
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
        # Store is constructed once and driven from both the scheduler tick
        # and the Slack websocket handler in the same process. sqlite3's
        # check_same_thread=False only lifts Python's same-thread assertion;
        # it adds no locking of its own, so every public method must hold
        # this lock across its full read-then-write sequence.
        self._lock = threading.Lock()

    def record_event(self, kind: str, payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO events (at, kind, payload) VALUES (?, ?, ?)",
                (_now(), kind, json.dumps(payload)),
            )
            self._db.commit()
            return int(cur.lastrowid or 0)

    def put_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO snapshots (at, body) VALUES (?, ?)", (_now(), json.dumps(snapshot))
            )
            self._db.commit()

    def last_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT body FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return json.loads(row[0]) if row else None

    def queue_pending(self, diff: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO pending (at, body) VALUES (?, ?)", (_now(), json.dumps(diff))
            )
            self._db.commit()

    def drain_pending(self) -> list[dict[str, Any]]:
        """Delete and return every queued diff, oldest first.

        Full, unconditional drain — used for test/manual introspection of the
        whole backlog. `Ticker.once` no longer uses this directly; it uses
        `peek_pending`/`delete_pending` so a queued diff is only deleted
        after a model call that actually consumed it succeeds.
        """
        with self._lock:
            rows = self._db.execute("SELECT id, body FROM pending ORDER BY id").fetchall()
            if not rows:
                return []
            self._db.execute("DELETE FROM pending WHERE id <= ?", (rows[-1][0],))
            self._db.commit()
            return [json.loads(body) for _, body in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) FROM pending").fetchone()
            return int(row[0])

    def peek_pending(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        """Return up to `limit` queued diffs, oldest first, without deleting them.

        Pairs with `delete_pending`: a caller should only delete rows it
        actually handed to the model and that model call actually
        succeeded, so a crash between reading and using the backlog never
        loses it.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, body FROM pending ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            return [(int(row_id), json.loads(body)) for row_id, body in rows]

    def delete_pending(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany(
                "DELETE FROM pending WHERE id = ?", [(i,) for i in ids]
            )
            self._db.commit()

    def get_thread(self, key: str) -> tuple[str, list[dict[str, Any]]] | None:
        """Return (channel, entries) stored for this conversation key, or
        None if nothing is stored yet.

        `key` is an opaque conversation identifier chosen by the caller
        (see `agent/conversation.py`) - Store itself has no opinion on what
        makes two messages "the same conversation", it just persists
        whatever blob it's handed under whatever key it's given, the same
        way `put_snapshot`/`queue_pending` persist opaque JSON bodies
        elsewhere in this class.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT channel, history FROM threads WHERE thread_ts = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return row[0], list(json.loads(row[1]))

    def save_thread(self, key: str, channel: str, entries: list[dict[str, Any]]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO threads (thread_ts, channel, history) VALUES (?, ?, ?) "
                "ON CONFLICT(thread_ts) DO UPDATE SET "
                "channel = excluded.channel, history = excluded.history",
                (key, channel, json.dumps(entries)),
            )
            self._db.commit()
