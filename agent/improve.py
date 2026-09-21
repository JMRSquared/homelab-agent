"""The 10-minute self-improvement cycle.

Separate entry point from `agent/main.py` (the Slack daemon and its 60s
`tick.py` sweep) - a different process, on its own systemd timer
(`deploy/homelab-improve.service`/`.timer`), sharing only the SQLite store
and the tool registry. It does not touch `tick.py` or anything scheduled
inside `main.py`'s process; see `docs/deploy.md`-style separation of
concerns.

One cycle: gather what's needed to judge the state of things, ask the model
for *one* worthwhile improvement (or none), let it act with the existing
tools, then make sure a report reaches Slack regardless of whether the
model remembered to post one itself.

Two properties this module leans on hard, both explained where they're
implemented rather than here:

- Self-modification survives a bad edit because the actual commit/test/
  restart/verify/rollback sequence is a standalone shell script
  (`deploy/self-deploy.sh`), not Python running inside the process that
  might get killed by its own restart - see that script's header and
  `agent/tools/selfops.py`.
- Two cycles can't run at once because of a `flock`-style lock file,
  checked first, before anything else - see `_acquire_lock` below. A
  cycle that finds the lock held exits immediately rather than queuing;
  the next timer tick 10 minutes later tries again.
"""

import argparse
import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from agent import config, slack_app
from agent.model import Agent
from agent.prompts import MT5_GUARDRAILS
from agent.store import Store
from agent.tick import collect_async

# Tool registration by import side effect - see agent/main.py's identical
# comment. selfops (self_deploy, hostctl_restart_verified) is the one
# module that exists only for this entry point; every other tool the
# improvement cycle might reasonably want (to look at Slack history, read
# its own brain, run guest_exec to inspect or edit a file) is the same
# registry the chat and tick paths already share. `comms` is also used
# directly below (slack_say, slack_history), not just for its registration
# side effect.
from agent.tools import (  # noqa: F401
    comms,
    household,
    infra,
    mail,
    media,
    memory,
    mt5_screenshot,
    photos,
    selfops,
    vision,
)

logger = logging.getLogger(__name__)

# Where the flock-style lock file lives. /run is tmpfs and cleared on
# reboot, which is exactly right for a lock whose only job is "is a cycle
# from *this boot* still running" - the same shape as the owner's own
# tazzie-status cron lock.
LOCK_PATH = os.environ.get("AGENT_IMPROVE_LOCK", "/run/lock/homelab-improve.lock")

# Wall-clock budget for the whole cycle's model call (gathering context is
# not included - that's local reads and a couple of quick HTTP calls, not
# something that needs its own cap). Generous enough for self_deploy's own
# internal ~480s ceiling to run to completion inside it in the common case,
# short enough that a stuck cycle doesn't run into the next timer tick.
WALL_CLOCK_TIMEOUT_S = float(os.environ.get("AGENT_IMPROVE_TIMEOUT_S", "540"))

# How many rows of each event kind to pull for context. Not the round cap -
# see AGENT_MAX_TOOL_ROUNDS in agent/model.py, set independently for this
# process via deploy/homelab-improve.service's own environment so the
# improvement cycle gets its own (tighter) round budget from the chat/tick
# path without agent/model.py needing to know two different callers want
# two different limits.
EVENT_LIMIT = 30
JOURNAL_LINES = 200

# The exact string the model must reply with, and only this, to mean "I
# looked and there's nothing worth changing" - same convention as
# tick.py's SYSTEM_DAEMON "idle". Checked case-insensitively against the
# stripped final answer.
NOTHING_TOKEN = "nothing needs changing"

SYSTEM_IMPROVE = (
    "You are the homelab agent's own self-improvement process, running as a "
    "separate one-shot cycle every 10 minutes - not the Slack chat assistant, "
    "and not the 60-second operational tick that watches for and fixes live "
    "problems (that loop is separate and off-limits to you; do not touch "
    "agent/tick.py or anything it depends on). Your job is different: look at "
    "how you (the same agent, same brain, same tools) have actually been doing, "
    "and make exactly ONE worthwhile improvement if you find one. Not a list, "
    "not several small tweaks - one focused, reviewable change. Six changes an "
    "hour is already a lot for a system nobody is approving each one of; a "
    "change you can't defend in one sentence isn't worth shipping.\n\n"
    "You have just been handed: recent tick events, recent tool-call failures, "
    "your own service's journal tail, current homelab state, and recent Slack "
    "activity in the channels you're in. Look specifically for:\n"
    "- a tool failure that keeps recurring, not a one-off\n"
    "- something someone asked for in Slack that you could not actually do\n"
    "- a gap in your own brain (agent/brain.py-backed notes) that made you "
    "guess or answer badly\n"
    "- a rough edge in how you reply - wrong channel, wrong tone, unclear, "
    "too technical for the audience, etc.\n"
    "- a homelab problem the 60-second tick is not catching (it only wakes on "
    "a state diff against a fixed set of collectors - something outside that "
    "set is invisible to it even if it's a real problem)\n\n"
    "'Nothing needs changing' is a genuinely good answer, not a failure to find "
    "something. If nothing you found clears the bar of 'worth a code change and "
    "a Slack post about it', call no tools at all and reply with exactly the "
    "words: nothing needs changing\n\n"
    "If you do find something worth doing: make the change yourself (guest_exec "
    "on guest 104 reaches your own checkout at /opt/homelab-agent to edit files; "
    "brain_write for a brain gap), then call self_deploy to commit, test, and "
    "ship it safely - never commit-and-restart by hand through guest_exec/"
    "host_exec directly, self_deploy is the only path that tests before "
    "restarting and rolls itself back automatically if the restart doesn't "
    "take. If self_deploy reports the test suite failed or it had to roll "
    "back, that is not a success - say so plainly. Only restart hostctl via "
    "hostctl_restart_verified, and only for a specific concrete reason - it "
    "has no automatic rollback if it doesn't come back.\n\n"
    "Whatever the outcome - shipped, rolled back, or nothing worth doing - post "
    f"one summary to {slack_app.CH_HOMELAB} using slack_say: what you changed "
    "and why, or that you looked and found nothing worth changing, or that a "
    "change failed its tests or had to roll back. This is the only report the "
    "owner sees for this cycle; make it plain enough for a non-engineer to "
    "follow, the same voice you'd use in #family. "
    + MT5_GUARDRAILS
)


class Runner(Protocol):
    """What `run_cycle` needs from an agent: run the model, and accept an
    audit callback. Deliberately its own protocol rather than reusing
    `tick.Runner` or `slack_app.Runner` (both just `run`) - `set_audit` is
    part of the contract here too, since every tool call this cycle makes
    must reach the audit trail the same as any other caller's, and a fake
    in a test needs to satisfy exactly that, no more."""

    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...
    def set_audit(self, audit: Callable[[str], Awaitable[None]]) -> None: ...


class _LockHeld:
    """RAII-ish holder for the flock'd lock file descriptor."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def release(self) -> None:
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _acquire_lock(path: str) -> _LockHeld | None:
    """Non-blocking flock on `path`. Returns None (skip, don't queue) if
    another cycle already holds it - the same "skip rather than queue"
    behaviour as `flock -n` on the owner's tazzie-status cron, just taken
    from Python so `run_cycle` can be unit-tested without shelling out.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return _LockHeld(fd)


def _recent_events(db_path: str, kind: str, limit: int) -> list[dict[str, Any]]:
    """Read the most recent `limit` events of `kind`, oldest of the batch
    first, straight from the `events` table.

    Opens its own short-lived connection rather than going through `Store`
    - `Store`'s public API has no "read recent events by kind" method, and
    adding one is out of scope here (agent/store.py is mid-edit for
    conversation memory in a separate, concurrent change). A fresh
    `sqlite3.connect` per call is exactly what `Store` itself does per
    *method* call under its own lock; the difference here is there's no
    in-process lock to share, because this reader is a different OS
    process from the one running `Store` against this file for real
    (`main.py`'s). That's fine: `sqlite3.connect`'s default 5-second
    `timeout` already waits out a competing writer's transaction instead of
    failing immediately, which is the actual protection two separate
    processes sharing one SQLite file get - not a lock either process can
    see the other's, but the driver-level busy wait around SQLite's own
    file lock. See the self-improve report for where this could still
    theoretically race.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT at, payload FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    out: list[dict[str, Any]] = []
    for at, payload in reversed(rows):
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            body = {"raw": payload}
        out.append({"at": at, **body})
    return out


def _recent_tool_failures(db_path: str, limit: int) -> list[dict[str, Any]]:
    """Tool-call events whose recorded result was not `ok`, most recent
    `limit`. Over-fetches tool_call events (failures are a minority of
    calls in normal operation) and filters locally - `events` has no
    column to filter this in SQL without parsing the JSON payload, and this
    table is small enough that doing it in Python is simpler than a JSON
    SQL function that may not exist in every SQLite build."""
    events = _recent_events(db_path, "tool_call", max(limit * 5, limit))
    failures = [
        e for e in events if isinstance(e.get("out"), dict) and not e["out"].get("ok", True)
    ]
    return failures[-limit:]


def _journal_tail(unit: str, lines: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {"stdout": proc.stdout[-8000:], "exitcode": proc.returncode}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _slack_snapshot(channels: list[str], per_channel_limit: int = 15) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for ch in channels:
        try:
            out[ch] = comms.slack_history(ch, limit=per_channel_limit)
        except Exception as exc:
            out[ch] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def gather(settings: config.Settings) -> dict[str, Any]:
    """Collect everything the model needs to judge the state of things.
    Nothing here mutates anything - purely read-only, safe to call as often
    as wanted."""
    state = await collect_async()
    journal = await asyncio.to_thread(_journal_tail, "homelab-agent", JOURNAL_LINES)
    slack_recent = await asyncio.to_thread(
        _slack_snapshot, [slack_app.CH_HOMELAB, slack_app.CH_LOG]
    )
    return {
        "homelab_state": state,
        "recent_ticks": _recent_events(settings.db_path, "tick", EVENT_LIMIT),
        "recent_tool_failures": _recent_tool_failures(settings.db_path, EVENT_LIMIT),
        "recent_slack_messages_to_agent": _recent_events(
            settings.db_path, "slack_message", EVENT_LIMIT
        ),
        "recent_slack_dms_to_agent": _recent_events(settings.db_path, "slack_dm", EVENT_LIMIT),
        "own_service_journal_tail": journal,
        "recent_slack_channel_activity": slack_recent,
    }


async def _post(channel: str, text: str) -> None:
    """Post to Slack via the plain `slack_say` tool function, off the event
    loop. `agent/main.py`'s `notify`/`audit` use the live Bolt app's async
    web client instead - this process is a short-lived batch job with no
    Socket Mode connection to hang one off, so it reuses the same
    already-tested HTTP path every tool call goes through anyway."""
    await asyncio.to_thread(comms.slack_say, channel, text)


async def run_cycle(settings: config.Settings, store: Store, agent: Runner | None = None) -> str:
    """Run exactly one improvement cycle to completion (or the wall-clock
    timeout) and guarantee a report reaches the status channel. Returns the
    model's final text for tests/logging; callers doing real work don't
    need the return value; the Slack post and the `improve_cycle` event
    are the durable record.

    `agent` defaults to a real `Agent(settings, store)`, built here rather
    than always passed in by the caller - same shape as `main.py` building
    the real `Agent` before handing it to `tick.Ticker`. Tests pass a fake
    `Runner` instead, the same way `tests/agent/test_tick.py` does, so they
    don't need a real MiniMax key or network access.
    """
    # Agent.run narrows `priority` to Literal["family", "daemon"] while this
    # module's Runner protocol (like tick.py's and slack_app.py's) declares
    # it as plain `str` - the same mismatch agent/main.py already casts
    # around; only "daemon" is ever passed below, so this is safe.
    runner: Runner
    if agent is not None:
        runner = agent
    else:
        runner = cast(Runner, Agent(settings, store))
    posted_status = False

    async def audit(text: str) -> None:
        nonlocal posted_status
        if text.startswith("`slack_say`") and slack_app.CH_HOMELAB in text:
            posted_status = True
        try:
            await _post(slack_app.CH_LOG, text)
        except Exception:
            logger.exception("improve cycle: audit post to log channel failed")

    runner.set_audit(audit)

    context = await gather(settings)
    prompt = json.dumps(context, indent=2, sort_keys=True, default=str)

    timed_out = False
    try:
        answer = await asyncio.wait_for(
            runner.run(prompt, priority="daemon", system=SYSTEM_IMPROVE),
            timeout=WALL_CLOCK_TIMEOUT_S,
        )
    except TimeoutError:
        timed_out = True
        answer = (
            "stopped: exceeded the improvement cycle's wall-clock budget "
            f"({WALL_CLOCK_TIMEOUT_S:.0f}s). Any change already in flight (for "
            "example inside self_deploy) keeps running to completion on its own - "
            "it does not depend on this process still being alive - so it may "
            "still finish shipping or rolling back after this message."
        )
    except Exception as exc:
        answer = f"improvement cycle failed before producing an answer: {type(exc).__name__}: {exc}"

    store.record_event(
        "improve_cycle", {"answer": answer, "timed_out": timed_out, "posted_status": posted_status}
    )

    nothing = answer.strip().lower() == NOTHING_TOKEN
    if not posted_status:
        fallback = (
            ":gear: nothing needs changing this cycle."
            if nothing
            else (
                ":gear: improvement cycle report (fallback - the model didn't "
                f"post its own):\n{answer}"
            )
        )
        try:
            await _post(slack_app.CH_HOMELAB, fallback)
        except Exception:
            logger.exception("improve cycle: fallback status post failed")

    return answer


async def amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock = _acquire_lock(LOCK_PATH)
    if lock is None:
        logging.info("another improvement cycle is still running (lock held), skipping")
        return 0
    try:
        settings = config.load()
        store = Store(settings.db_path)
        await run_cycle(settings, store)
    finally:
        lock.release()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
