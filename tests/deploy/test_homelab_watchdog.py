"""Exercises deploy/homelab-watchdog.sh directly, as a subprocess, against
fake `pct` and a fake `notify-slack.sh` - not the real ones or a real
Slack. The point under test: it alerts on the transition into and out of a
bad state, stays silent on a fresh healthy first run and on repeated runs
in the same state, and can tell "container unreachable" apart from
"container reachable, service just isn't active".
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "homelab-watchdog.sh"


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_FAKE_PCT = """#!/usr/bin/env bash
mode="${FAKE_PCT_MODE:-healthy}"
# expected invocation shape: pct exec <ct> -- <remote argv...>
shift  # drop "exec"
shift  # drop <ct>
shift  # drop "--"
remote="$1"

if [ "$mode" = "unreachable" ]; then
    echo "pct: unix socket error: container is not running" >&2
    exit 1
fi

case "$remote" in
  systemctl)
    if [ "$mode" = "not_active" ]; then
      echo "inactive"
      exit 3
    fi
    echo "active"
    exit 0
    ;;
  journalctl)
    if [ "$mode" = "silent" ]; then
      # --since query returns nothing; -n tail still has old lines
      if printf '%s\\n' "$@" | grep -q -- "--since"; then
        exit 0
      fi
      echo "old log line from a while ago"
      exit 0
    fi
    echo "log line 1"
    echo "log line 2"
    exit 0
    ;;
esac
exit 0
"""


@pytest.fixture()
def fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    _executable(bindir / "pct", _FAKE_PCT)
    # A stand-in for notify-slack.sh that just records each call's argv
    # (message text) to a file, one line per call - the watchdog script
    # locates it next to itself via $SCRIPT_DIR, so the real script under
    # test is copied into this fake bin dir alongside the stub.
    return bindir


@pytest.fixture()
def sandbox(tmp_path: Path, fake_bin: Path) -> Path:
    """A private copy of deploy/ so the watchdog script's `$SCRIPT_DIR`
    resolution finds a stubbed notify-slack.sh instead of shelling out for
    real."""
    deploy_copy = tmp_path / "deploy"
    deploy_copy.mkdir()
    script_copy = deploy_copy / "homelab-watchdog.sh"
    script_copy.write_text(SCRIPT.read_text())
    script_copy.chmod(script_copy.stat().st_mode | stat.S_IEXEC)

    # Each call's full (possibly multi-line) message is written between a
    # marker line, so a message containing its own newlines doesn't get
    # miscounted as several calls when the test reads this file back.
    calls_file = tmp_path / "notify-calls.txt"
    _executable(
        deploy_copy / "notify-slack.sh",
        f"""#!/usr/bin/env bash
{{ echo "===CALL-START==="; printf '%s\\n' "$1"; echo "===CALL-END==="; }} >> "{calls_file}"
exit 0
""",
    )
    return tmp_path


def _run(
    sandbox: Path, fake_bin: Path, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["HOMELAB_WATCHDOG_ENV_FILE"] = str(sandbox / "does-not-exist-env")
    env["HOMELAB_WATCHDOG_STATE_FILE"] = str(sandbox / "state")
    env["HOMELAB_WATCHDOG_STALE_MINUTES"] = "15"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(sandbox / "deploy" / "homelab-watchdog.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _calls(sandbox: Path) -> list[str]:
    """One entry per notify-slack.sh invocation, each the full message text
    (which may itself span several lines)."""
    calls_file = sandbox / "notify-calls.txt"
    if not calls_file.exists():
        return []
    text = calls_file.read_text()
    calls = []
    for block in text.split("===CALL-START===\n")[1:]:
        calls.append(block.split("===CALL-END===\n", 1)[0])
    return calls


def test_first_run_healthy_seeds_state_silently(sandbox, fake_bin):
    proc = _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    assert proc.returncode == 0
    assert _calls(sandbox) == []
    assert (sandbox / "state").exists()


def test_repeated_healthy_runs_stay_silent(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    proc = _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    assert proc.returncode == 0
    assert _calls(sandbox) == []


def test_transition_to_unreachable_alerts_once(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    calls = _calls(sandbox)
    assert len(calls) == 1
    assert "rotating_light" in calls[0]
    assert "unreachable" in calls[0] or "container" in calls[0]


def test_staying_unreachable_does_not_alert_again(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    assert len(_calls(sandbox)) == 1


def test_service_not_active_alerts_and_names_the_state(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="not_active")
    calls = _calls(sandbox)
    assert len(calls) == 1
    assert "not active" in calls[0] or "inactive" in calls[0]


def test_silent_journal_is_treated_as_down(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="silent")
    calls = _calls(sandbox)
    assert len(calls) == 1
    assert "wedged" in calls[0] or "no journal" in calls[0]


def test_recovery_after_down_alerts_once(sandbox, fake_bin):
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    _run(sandbox, fake_bin, FAKE_PCT_MODE="healthy")
    calls = _calls(sandbox)
    assert len(calls) == 2
    assert "rotating_light" in calls[0]
    assert "white_check_mark" in calls[1]
    assert "back" in calls[1]


def test_first_run_down_alerts_without_a_known_duration(sandbox, fake_bin):
    proc = _run(sandbox, fake_bin, FAKE_PCT_MODE="unreachable")
    assert proc.returncode == 0
    calls = _calls(sandbox)
    assert len(calls) == 1
    assert "first watchdog run" in calls[0]
