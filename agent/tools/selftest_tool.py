"""Exposes `agent/selftest.py`'s reachability suite as a tool, so both the
improvement cycle and a Slack "are all your tools working?" question can
run it.

Named `selftest_tool` rather than `selftest` so it doesn't shadow the
`agent.selftest` module it wraps - `agent/tools/*.py` modules are imported
by name elsewhere (see `agent/main.py`, `agent/improve.py`) and a same-name
module and package importable from different roots is exactly the kind of
thing that's fine until someone's editor or `mypy` picks the wrong one.
"""

from typing import Any

from agent import selftest
from agent.tools.base import tool


@tool(
    "self_test",
    "Run a fast, read-only reachability check across every tool that talks to "
    "something external: hostctl (and everything behind it - guests, ZFS, docker), "
    "Uptime Kuma, AdGuard, Jellyfin/Jellyseerr, Immich, mail (IMAP and SMTP), Slack, "
    "and the MiniMax API. Use this to answer 'are all your tools working?' or to "
    "check your own capabilities are still intact. Each dependency is reported as "
    "one of: ok (answered with real data), empty (answered but with nothing in it - "
    "treat this as suspicious, not healthy, the same as a failure until you've "
    "confirmed it's expected), failed (the call itself errored, with why), or "
    "unavailable (a required key or password isn't configured - that's a "
    "configuration gap, not a fault). Safe to run any time: every probe is read-only "
    "or a connect-and-authenticate check with nothing sent - it never snapshots, "
    "restarts, sends mail, or places anything.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def self_test() -> dict[str, Any]:
    return selftest.run_self_test()
