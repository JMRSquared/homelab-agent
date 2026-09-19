"""Read-only MT5 (VM 200) account status from the TazzieMoney EA's own
on-disk export, without touching the VM.

/tank/dev/tazzie-metrics/tazzie.db is populated on the Proxmox host by an
existing cron job (tazzie-export.py) that reads a heartbeat CSV the EA
itself writes inside the terminal, via `qm guest exec`, and by a second
OCR-based screenshot check. This module only reads that already-collected
data - it never calls `qm guest exec` itself, so `mt5_status()` answers "how
many positions are open" and "what's my balance" without touching the
terminal at all, which is faster and safer than driving MetaTrader for a
question that a file on the host can already answer.
"""

import sqlite3
import time
from typing import Any

DB_PATH = "/tank/dev/tazzie-metrics/tazzie.db"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["age_s"] = int(time.time()) - int(d["ts_epoch"])
    return d


def status() -> dict[str, Any]:
    """Latest known MT5 account state.

    Two data sources feed the `equity` table and can go stale or disagree
    independently (confirmed live: the EA heartbeat source was over 100
    hours stale while an OCR-based check reported fresher equity/balance
    minutes old), so both are reported rather than merged into one number a
    caller can't audit:
    - `latest`: the most recent row of any kind - usually the freshest
      equity/balance reading.
    - `latest_with_open_positions`: the most recent row that actually
      reports `open_positions` - from the EA's own file heartbeat, which
      can be far staler. `hb_age_s` is the heartbeat file's own age as MT5
      last reported it, distinct from `age_s` (this row's age in the DB).
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        latest = conn.execute("SELECT * FROM equity ORDER BY ts_epoch DESC LIMIT 1").fetchone()
        latest_with_positions = conn.execute(
            "SELECT * FROM equity WHERE open_positions IS NOT NULL "
            "ORDER BY ts_epoch DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    return {
        "latest": _row_to_dict(latest) if latest else None,
        "latest_with_open_positions": (
            _row_to_dict(latest_with_positions) if latest_with_positions else None
        ),
        "note": (
            "Two independent sources feed this data and can go stale or disagree "
            "with each other - 'latest' is usually fresher for equity/balance, "
            "'latest_with_open_positions' is the EA's own file heartbeat and can be "
            "far older (check its age_s and hb_age_s). A large age_s is a reason to "
            "say the data looks stale, not to report it as current."
        ),
    }
