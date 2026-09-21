"""Exercises deploy/self-deploy.sh directly, as a subprocess, against a
throwaway git repo and fake `systemctl`/`journalctl` - not the real ones.

This is the "one hard engineering problem" from the task: the script has
to (1) refuse to restart a broken change and revert it, and (2) roll back
automatically if a restart doesn't come back cleanly. Both are tested here
against the actual shell script, not a Python re-implementation of its
logic - a passing test that only exercised a Python model of the script
could pass while the real script (what actually runs in production) still
had a bug.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "self-deploy.sh"

_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
mode="${FAKE_SYSTEMCTL_MODE:-always_active}"
cmd="$1"; shift || true
case "$cmd" in
  restart)
    exit 0
    ;;
  is-active)
    quiet=0
    if [ "${1:-}" = "--quiet" ]; then quiet=1; shift || true; fi
    if [ "$mode" = "always_active" ]; then
      [ "$quiet" -eq 1 ] || echo "active"
      exit 0
    else
      [ "$quiet" -eq 1 ] || echo "inactive"
      exit 3
    fi
    ;;
  *)
    exit 0
    ;;
esac
"""

_FAKE_JOURNALCTL = """#!/usr/bin/env bash
if [ -n "${FAKE_JOURNAL_FILE:-}" ] && [ -f "${FAKE_JOURNAL_FILE:-}" ]; then
  cat "$FAKE_JOURNAL_FILE"
fi
exit 0
"""


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_repo(tmp_path: Path, *, pytest_passes: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "a@b.c"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    (repo / "agent").mkdir()
    (repo / "hostctl").mkdir()
    (repo / "tests").mkdir()
    (repo / "agent" / "dummy.py").write_text("VALUE = 1\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "baseline"], check=True)

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    exit_code = "0" if pytest_passes else "1"
    _executable(venv_bin / "pytest", f"#!/usr/bin/env bash\nexit {exit_code}\n")
    _executable(venv_bin / "ruff", "#!/usr/bin/env bash\nexit 0\n")
    _executable(venv_bin / "mypy", "#!/usr/bin/env bash\nexit 0\n")
    _executable(venv_bin / "pip", "#!/usr/bin/env bash\nexit 0\n")
    return repo


@pytest.fixture()
def fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    _executable(bindir / "systemctl", _FAKE_SYSTEMCTL)
    _executable(bindir / "journalctl", _FAKE_JOURNALCTL)
    return bindir


def _run(
    repo: Path, fake_bin: Path, msg_file: Path, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["SELF_DEPLOY_REPO_DIR"] = str(repo)
    env["SELF_DEPLOY_SERVICE"] = "fake-service"
    env["SELF_DEPLOY_VERIFY_TIMEOUT_S"] = "6"
    env["SELF_DEPLOY_VERIFY_INTERVAL_S"] = "1"
    env["SELF_DEPLOY_STABLE_CHECKS"] = "2"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), str(msg_file)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _result_json(proc: subprocess.CompletedProcess[str]) -> dict:
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT_JSON: "):
            return json.loads(line[len("RESULT_JSON: ") :])
    raise AssertionError(f"no RESULT_JSON line in stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def test_failing_test_suite_blocks_the_restart_and_reverts(tmp_path, fake_bin):
    repo = _make_repo(tmp_path, pytest_passes=False)
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "agent" / "dummy.py").write_text("VALUE = 2  # a bad self-edit\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("self-improve: tweak dummy value\n")

    proc = _run(repo, fake_bin, msg_file, FAKE_SYSTEMCTL_MODE="always_active")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "tests"
    # Reverted: HEAD is back to the pre-change commit, and the working tree
    # no longer carries the bad edit.
    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert post_sha == pre_sha
    assert (repo / "agent" / "dummy.py").read_text() == "VALUE = 1\n"


def test_service_not_coming_back_triggers_rollback(tmp_path, fake_bin):
    repo = _make_repo(tmp_path, pytest_passes=True)
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "agent" / "dummy.py").write_text("VALUE = 2  # ships fine, service won't come back\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("self-improve: tweak dummy value\n")

    proc = _run(repo, fake_bin, msg_file, FAKE_SYSTEMCTL_MODE="never_active")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["rolled_back"] is True
    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert post_sha == pre_sha
    assert (repo / "agent" / "dummy.py").read_text() == "VALUE = 1\n"


def test_successful_deploy_commits_tests_and_restarts(tmp_path, fake_bin):
    repo = _make_repo(tmp_path, pytest_passes=True)
    pre_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    (repo / "agent" / "dummy.py").write_text("VALUE = 2  # a good self-edit\n")
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("self-improve: tweak dummy value\n\nbecause it was wrong.\n")

    proc = _run(repo, fake_bin, msg_file, FAKE_SYSTEMCTL_MODE="always_active")
    result = _result_json(proc)

    assert result["ok"] is True
    assert result["rolled_back"] is False
    assert result["pre_sha"] == pre_sha
    assert result["pushed"] is False  # no `origin` remote configured in the test repo
    post_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert post_sha == result["sha"]
    assert post_sha != pre_sha


def test_no_changes_reports_failure_without_touching_the_service(tmp_path, fake_bin):
    repo = _make_repo(tmp_path, pytest_passes=True)
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("self-improve: nothing actually changed\n")

    proc = _run(repo, fake_bin, msg_file, FAKE_SYSTEMCTL_MODE="always_active")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "commit"
