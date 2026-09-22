"""Long-running commands.

The bug these cover: `docker compose pull` outruns the 300-second
synchronous exec path, the tool returns a timeout, and the agent reports a
failure for a pull that is still downloading - then sometimes starts it
again. A job that is still running must read as "still running", never as
a failure, and must always be reachable by id.
"""

import json

import httpx
import pytest
import respx

from agent.tools import base, infra
from agent.tools import jobs as jobs_tool  # noqa: F401  (registers job_* tools)

HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")


@respx.mock
def test_job_start_returns_an_id_immediately():
    route = respx.post(f"{HOSTCTL}/jobs").mock(
        return_value=httpx.Response(
            201, json={"job_id": "j-7f2", "status": "running", "started_at": "2026-09-22T12:00:00Z"}
        )
    )
    out = base.dispatch("job_start", {"target": "101", "command": "docker compose pull"})
    assert out["ok"] is True
    assert out["result"]["job_id"] == "j-7f2"
    assert out["result"]["still_running"] is True
    assert out["result"]["finished"] is False
    assert json.loads(route.calls.last.request.read()) == {
        "target": "101",
        "command": "docker compose pull",
    }


@respx.mock
def test_a_running_job_reports_progress_not_a_failure():
    """The whole point: nine minutes into a large pull, the model must be
    able to tell 'still going' from 'broken'."""
    respx.get(f"{HOSTCTL}/jobs/j-7f2").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "j-7f2",
                "target": "101",
                "command": "docker compose pull",
                "status": "running",
                "exitcode": None,
                "runtime_s": 540,
                "stdout_tail": "jellyfin Pulling\n8f2a1c: Downloading [====>    ] 412MB/1.1GB\n",
                "stderr_tail": "",
            },
        )
    )
    result = base.dispatch("job_status", {"job_id": "j-7f2"})["result"]
    assert result["status"] == "running"
    assert result["still_running"] is True
    assert result["finished"] is False
    assert result["exitcode"] is None
    assert "Downloading" in result["stdout_tail"]
    assert result["runtime_s"] == 540


@respx.mock
def test_a_finished_job_reports_its_exit_code():
    respx.get(f"{HOSTCTL}/jobs/j-7f2").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "j-7f2",
                "status": "succeeded",
                "exitcode": 0,
                "runtime_s": 812,
                "stdout_tail": "jellyfin Pulled\n",
                "stderr_tail": "",
            },
        )
    )
    result = base.dispatch("job_status", {"job_id": "j-7f2"})["result"]
    assert result["still_running"] is False
    assert result["finished"] is True
    assert result["exitcode"] == 0


@respx.mock
def test_a_failed_job_is_distinguishable_from_a_running_one():
    respx.get(f"{HOSTCTL}/jobs/j-bad").mock(
        return_value=httpx.Response(
            200,
            json={
                "job_id": "j-bad",
                "status": "failed",
                "exitcode": 1,
                "stdout_tail": "",
                "stderr_tail": "manifest unknown\n",
            },
        )
    )
    result = base.dispatch("job_status", {"job_id": "j-bad"})["result"]
    assert (result["still_running"], result["finished"], result["exitcode"]) == (False, True, 1)
    assert "manifest unknown" in result["stderr_tail"]


@respx.mock
def test_job_list_finds_work_the_conversation_lost_track_of():
    respx.get(f"{HOSTCTL}/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"job_id": "j-7f2", "status": "running", "command": "docker compose pull"},
                    {"job_id": "j-old", "status": "succeeded", "exitcode": 0},
                ]
            },
        )
    )
    jobs = base.dispatch("job_list", {})["result"]["jobs"]
    assert [j["job_id"] for j in jobs] == ["j-7f2", "j-old"]
    assert jobs[0]["still_running"] is True


@respx.mock
def test_job_status_output_is_capped_like_exec_output():
    respx.get(f"{HOSTCTL}/jobs/j-big").mock(
        return_value=httpx.Response(
            200, json={"job_id": "j-big", "status": "running", "stdout_tail": "x" * 10_000}
        )
    )
    result = base.dispatch("job_status", {"job_id": "j-big"})["result"]
    assert result["stdout_truncated"] is True
    assert result["stdout_total_chars"] == 10_000
    assert len(result["stdout_tail"]) == infra._EXEC_OUTPUT_LIMIT


def test_a_bad_job_id_is_rejected_before_it_reaches_hostctl():
    out = base.dispatch("job_status", {"job_id": "../../etc/passwd"})
    assert out["ok"] is False


@respx.mock
def test_docker_pull_runs_as_a_job_rather_than_a_five_minute_gamble():
    route = respx.post(f"{HOSTCTL}/jobs").mock(
        return_value=httpx.Response(201, json={"job_id": "j-pull", "status": "running"})
    )
    result = base.dispatch("docker_action", {"stack": "debrid", "action": "pull"})["result"]
    assert result["job_id"] == "j-pull"
    assert result["still_running"] is True
    body = json.loads(route.calls.last.request.read())
    assert body["target"] == "101"
    assert body["command"] == (
        "docker compose -f /opt/stacks/debrid/compose.yaml pull"
    )


@respx.mock
def test_quick_docker_actions_still_run_inline():
    route = respx.post(f"{HOSTCTL}/guest/101/exec").mock(
        return_value=httpx.Response(200, json={"exitcode": 0, "stdout": "", "stderr": ""})
    )
    base.dispatch("docker_action", {"stack": "debrid", "action": "restart"})
    assert route.call_count == 1


@respx.mock
def test_a_synchronous_exec_timeout_is_not_reported_as_a_failure():
    """Regression test for the false failure: hostctl keeps running the
    command after the agent stops waiting, so the tool must say so and
    point at the job tools instead of letting the model call it dead."""
    respx.post(f"{HOSTCTL}/guest/101/shell").mock(
        side_effect=httpx.ReadTimeout("timed out", request=httpx.Request("POST", HOSTCTL))
    )
    out = base.dispatch("guest_exec", {"guest": 101, "command": "docker compose pull"})
    assert out["ok"] is False
    assert "still running" in out["error"]
    assert "NOT a failure" in out["error"]
    assert "job_start" in out["error"]


@respx.mock
def test_a_host_exec_timeout_says_the_same_thing():
    respx.post(f"{HOSTCTL}/host/exec").mock(
        side_effect=httpx.ReadTimeout("timed out", request=httpx.Request("POST", HOSTCTL))
    )
    out = base.dispatch("host_exec", {"command": "zfs send tank/immich | ..."})
    assert out["ok"] is False
    assert "still running" in out["error"] and "job_start" in out["error"]
