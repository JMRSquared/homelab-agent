import stat
from pathlib import Path

import httpx
import pytest
import respx

from agent.tools import base, selfops  # noqa: F401 - selfops registers on import

AGENT_HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", AGENT_HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")


def _fake_script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_self_deploy_parses_the_result_json_line(tmp_path, monkeypatch):
    script = tmp_path / "self-deploy.sh"
    _fake_script(
        script,
        "#!/usr/bin/env bash\n"
        'echo "commit message file: $1"\n'
        'echo "RESULT_JSON: {\\"ok\\": true, \\"stage\\": \\"done\\", \\"sha\\": \\"abc123\\", '
        '\\"pushed\\": false, \\"rolled_back\\": false}"\n',
    )
    monkeypatch.setattr(selfops, "_SELF_DEPLOY_SCRIPT", script)

    out = base.dispatch("self_deploy", {"commit_message": "fix the thing"})

    assert out["ok"] is True
    assert out["result"]["ok"] is True
    assert out["result"]["sha"] == "abc123"
    assert "log_tail" in out["result"]


def test_self_deploy_surfaces_a_rollback_as_a_tool_failure(tmp_path, monkeypatch):
    script = tmp_path / "self-deploy.sh"
    _fake_script(
        script,
        "#!/usr/bin/env bash\n"
        'echo "RESULT_JSON: {\\"ok\\": false, \\"stage\\": \\"verify\\", \\"rolled_back\\": true, '
        '\\"reason\\": \\"service did not come back\\"}"\n'
        "exit 1\n",
    )
    monkeypatch.setattr(selfops, "_SELF_DEPLOY_SCRIPT", script)

    out = base.dispatch("self_deploy", {"commit_message": "risky change"})

    # dispatch() wraps tool functions and re-raises them as ok:false only
    # when the *Python* call raises - self_deploy itself does not raise on
    # a script-reported failure, it returns the parsed result verbatim so
    # the model sees `ok: false, rolled_back: true` and can report it
    # accurately, rather than getting an opaque exception.
    assert out["ok"] is True
    assert out["result"]["ok"] is False
    assert out["result"]["rolled_back"] is True


def test_self_deploy_raises_clearly_when_no_result_line_is_printed(tmp_path, monkeypatch):
    script = tmp_path / "self-deploy.sh"
    _fake_script(script, "#!/usr/bin/env bash\necho 'something went sideways'\nexit 1\n")
    monkeypatch.setattr(selfops, "_SELF_DEPLOY_SCRIPT", script)

    out = base.dispatch("self_deploy", {"commit_message": "x"})

    assert out["ok"] is False
    assert "no parseable result" in out["error"]


@respx.mock
def test_hostctl_restart_verified_reports_came_back(monkeypatch):
    monkeypatch.setattr(selfops, "_HOSTCTL_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(selfops, "_HOSTCTL_VERIFY_TIMEOUT_S", 1.0)
    respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(
        side_effect=httpx.ConnectError("connection reset, as expected mid-restart")
    )
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(
        return_value=httpx.Response(200, json={"guests": []})
    )

    out = base.dispatch("hostctl_restart_verified", {})

    assert out["ok"] is True
    assert out["result"]["came_back"] is True


@respx.mock
def test_hostctl_restart_verified_reports_failure_plainly_when_it_never_comes_back(monkeypatch):
    monkeypatch.setattr(selfops, "_HOSTCTL_VERIFY_INTERVAL_S", 0.01)
    monkeypatch.setattr(selfops, "_HOSTCTL_VERIFY_TIMEOUT_S", 0.05)
    respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(
        side_effect=httpx.ConnectError("connection reset")
    )
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(side_effect=httpx.ConnectError("still down"))

    out = base.dispatch("hostctl_restart_verified", {})

    assert out["ok"] is True
    assert out["result"]["came_back"] is False
    assert "no automatic rollback" in out["result"]["warning"]
