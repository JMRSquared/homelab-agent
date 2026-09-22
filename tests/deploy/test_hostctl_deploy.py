"""Exercises deploy/hostctl-deploy.sh directly, as a subprocess, against a
throwaway git repo (with a real `origin` remote to pull from) and fake
`systemctl`/`pct`/pip - not the real ones.

Mirrors tests/agent/test_self_deploy.py's approach for the agent's own
self-deploy.sh: the script under test is the actual shell script that would
run in production, not a Python re-implementation of its logic.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hostctl-deploy.sh"

_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
exit 0
"""

# `pct exec <guest> -- curl ...` is the only invocation hostctl-deploy.sh
# makes against `pct`. Answers with $FAKE_PCT_HTTP_CODE (default 200) for
# any curl call, so the test controls what "the verification request"
# reports without a real hostctl or a real container.
_FAKE_PCT = """#!/usr/bin/env bash
code="${FAKE_PCT_HTTP_CODE:-200}"
if [ "$1" = "exec" ]; then
    printf '%s' "$code"
    exit 0
fi
exit 0
"""


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _make_origin_and_clone(tmp_path: Path, *, pip_fails: bool = False) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "a@b.c")
    _git(seed, "config", "user.name", "Test")
    (seed / "hostctl").mkdir()
    (seed / "hostctl" / "app.py").write_text("VALUE = 1\n")
    (seed / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    # Mirrors the real repo's own .gitignore (.venv/ is untracked there too)
    # - without it, the venv this test creates below would make the
    # checkout look dirty to hostctl-deploy.sh's own safety check.
    (seed / ".gitignore").write_text(".venv/\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "baseline")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(origin), str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "Test")

    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    pip_exit = "1" if pip_fails else "0"
    _executable(venv_bin / "pip", f"#!/usr/bin/env bash\nexit {pip_exit}\n")

    return origin, repo


def _push_new_commit(tmp_path: Path, origin: Path) -> str:
    """Simulate a new commit landing on GitHub after the hostctl checkout
    was made, via a second independent clone - exactly how a real deploy
    would see new upstream history."""
    pusher = tmp_path / "pusher"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(origin), str(pusher)],
        check=True,
        capture_output=True,
    )
    _git(pusher, "config", "user.email", "a@b.c")
    _git(pusher, "config", "user.name", "Test")
    (pusher / "hostctl" / "app.py").write_text("VALUE = 2  # new release\n")
    _git(pusher, "add", "-A")
    _git(pusher, "commit", "-q", "-m", "ship a change")
    _git(pusher, "push", "-q", "origin", "main")
    return _git(pusher, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def fake_bin(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    _executable(bindir / "systemctl", _FAKE_SYSTEMCTL)
    _executable(bindir / "pct", _FAKE_PCT)
    return bindir


def _run(
    repo: Path, fake_bin: Path, token_file: Path, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["HOSTCTL_DEPLOY_REPO_DIR"] = str(repo)
    env["HOSTCTL_DEPLOY_SERVICE"] = "fake-hostctl"
    env["HOSTCTL_DEPLOY_TOKEN_FILE"] = str(token_file)
    env["HOSTCTL_DEPLOY_VERIFY_TIMEOUT_S"] = "6"
    env["HOSTCTL_DEPLOY_VERIFY_INTERVAL_S"] = "1"
    env["HOSTCTL_DEPLOY_STABLE_CHECKS"] = "2"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60
    )


def _result_json(proc: subprocess.CompletedProcess[str]) -> dict:
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("RESULT_JSON: "):
            return json.loads(line[len("RESULT_JSON: ") :])
    raise AssertionError(f"no RESULT_JSON line in stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


def test_successful_deploy_pulls_installs_and_verifies(tmp_path, fake_bin):
    origin, repo = _make_origin_and_clone(tmp_path)
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    new_sha = _push_new_commit(tmp_path, origin)
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")

    proc = _run(repo, fake_bin, token_file, FAKE_PCT_HTTP_CODE="200")
    result = _result_json(proc)

    assert result["ok"] is True
    assert result["rolled_back"] is False
    assert result["pre_sha"] == pre_sha
    assert result["sha"] == new_sha
    post_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert post_sha == new_sha
    assert (repo / "hostctl" / "app.py").read_text() == "VALUE = 2  # new release\n"


def test_verification_failure_rolls_back(tmp_path, fake_bin):
    origin, repo = _make_origin_and_clone(tmp_path)
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _push_new_commit(tmp_path, origin)
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")

    proc = _run(repo, fake_bin, token_file, FAKE_PCT_HTTP_CODE="500")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "verify"
    assert result["rolled_back"] is True
    post_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert post_sha == pre_sha
    assert (repo / "hostctl" / "app.py").read_text() == "VALUE = 1\n"


def test_pip_install_failure_rolls_back(tmp_path, fake_bin):
    origin, repo = _make_origin_and_clone(tmp_path, pip_fails=True)
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _push_new_commit(tmp_path, origin)
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")

    proc = _run(repo, fake_bin, token_file, FAKE_PCT_HTTP_CODE="200")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "install"
    post_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert post_sha == pre_sha


def test_no_upstream_changes_still_verifies_and_succeeds(tmp_path, fake_bin):
    _origin, repo = _make_origin_and_clone(tmp_path)
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")

    proc = _run(repo, fake_bin, token_file, FAKE_PCT_HTTP_CODE="200")
    result = _result_json(proc)

    assert result["ok"] is True
    assert result["pre_sha"] == pre_sha
    assert result["sha"] == pre_sha


def test_refuses_to_run_against_a_non_git_directory(tmp_path, fake_bin):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")

    proc = _run(repo, fake_bin, token_file)
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "setup"


def test_missing_token_file_fails_verify_and_rolls_back(tmp_path, fake_bin):
    origin, repo = _make_origin_and_clone(tmp_path)
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _push_new_commit(tmp_path, origin)
    token_file = tmp_path / "does-not-exist"

    proc = _run(repo, fake_bin, token_file, FAKE_PCT_HTTP_CODE="200")
    result = _result_json(proc)

    assert result["ok"] is False
    assert result["stage"] == "verify"
    post_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert post_sha == pre_sha
