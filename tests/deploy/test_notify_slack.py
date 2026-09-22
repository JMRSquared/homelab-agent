"""Exercises deploy/notify-slack.sh directly, as a subprocess, against a
fake `curl` - not the real Slack API. Both deploy/homelab-agent-alert.sh
(inside LXC 104) and deploy/homelab-watchdog.sh (on the Proxmox host) shell
out to this script, so its own contract - what it reads from the
environment, what makes it succeed or fail - is worth pinning down
directly.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "notify-slack.sh"


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def fake_curl_capture(tmp_path: Path):
    """A fake `curl` that records the args it was called with (to a file,
    so the test can inspect them) and answers based on
    $FAKE_CURL_RESPONSE / $FAKE_CURL_EXIT."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    capture_file = tmp_path / "curl-args.txt"
    _executable(
        bindir / "curl",
        f"""#!/usr/bin/env bash
printf '%s\\n' "$@" > "{capture_file}"
exit_code="${{FAKE_CURL_EXIT:-0}}"
if [ "$exit_code" != "0" ]; then
    echo "curl: fake failure" >&2
    exit "$exit_code"
fi
printf '%s' "${{FAKE_CURL_RESPONSE:-{{\\"ok\\":true}}}}"
exit 0
""",
    )
    return bindir, capture_file


def _run(fake_bin: Path, message: str, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(SCRIPT), message], capture_output=True, text=True, env=env, timeout=30
    )


def test_posts_message_with_token_and_default_channel(fake_curl_capture):
    bindir, capture_file = fake_curl_capture
    proc = _run(bindir, "hello there", SLACK_BOT_TOKEN="xoxb-test")
    assert proc.returncode == 0
    args = capture_file.read_text()
    assert "Authorization: Bearer xoxb-test" in args
    assert "channel=#homelab-alerts" in args
    assert "text=hello there" in args


def test_respects_slack_channel_override(fake_curl_capture):
    bindir, capture_file = fake_curl_capture
    proc = _run(bindir, "hi", SLACK_BOT_TOKEN="xoxb-test", SLACK_CHANNEL="#agent-log")
    assert proc.returncode == 0
    assert "channel=#agent-log" in capture_file.read_text()


def test_missing_token_fails_without_calling_curl(fake_curl_capture):
    bindir, capture_file = fake_curl_capture
    env = dict(os.environ)
    env.pop("SLACK_BOT_TOKEN", None)
    proc = _run(bindir, "hi")
    assert proc.returncode == 1
    assert "SLACK_BOT_TOKEN" in proc.stderr
    assert not capture_file.exists()


def test_missing_message_is_a_usage_error(fake_curl_capture):
    bindir, _capture_file = fake_curl_capture
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SLACK_BOT_TOKEN": "x"},
        timeout=30,
    )
    assert proc.returncode == 2


def test_curl_network_failure_is_reported(fake_curl_capture):
    bindir, _capture_file = fake_curl_capture
    proc = _run(bindir, "hi", SLACK_BOT_TOKEN="xoxb-test", FAKE_CURL_EXIT="7")
    assert proc.returncode == 1
    assert "curl failed" in proc.stderr


def test_slack_api_error_body_is_reported(fake_curl_capture):
    bindir, _capture_file = fake_curl_capture
    proc = _run(
        bindir,
        "hi",
        SLACK_BOT_TOKEN="xoxb-test",
        FAKE_CURL_RESPONSE=json.dumps({"ok": False, "error": "channel_not_found"}),
    )
    assert proc.returncode == 1
    assert "channel_not_found" in proc.stderr
