import json

import httpx
import pytest
import respx

from agent.tools import base, infra  # noqa: F401

AGENT_HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", AGENT_HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")


@respx.mock
def test_guests_list_calls_hostctl():
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(
        return_value=httpx.Response(200, json={"guests": [{"id": 101}]})
    )
    assert base.dispatch("guests_list", {}) == {"ok": True, "result": {"guests": [{"id": 101}]}}


@respx.mock
def test_guest_action_forwards_action():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/105/action").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    base.dispatch("guest_action", {"guest": 105, "action": "reboot"})
    assert json.loads(route.calls.last.request.read()) == {"action": "reboot"}


@respx.mock
def test_guest_action_reaches_guest_200():
    """Regression test for VM 200 parity: the owner gave the agent full
    administrative control over the trading VM, reversing the old blanket
    rejection. guest_action(200, ...) must reach hostctl like any other
    guest id, not be rejected at the tool level."""
    route = respx.post(f"{AGENT_HOSTCTL}/guest/200/action").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    out = base.dispatch("guest_action", {"guest": 200, "action": "stop"})
    assert out["ok"] is True
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.read()) == {"action": "stop"}


@respx.mock
def test_guest_exec_reaches_the_shell_route():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/200/shell").mock(
        return_value=httpx.Response(
            200, json={"guest": 200, "stdout": "MT5\r\n", "stderr": "", "exitcode": 0}
        )
    )
    out = base.dispatch("guest_exec", {"guest": 200, "command": "hostname"})
    assert out["ok"] is True
    assert out["result"]["stdout"] == "MT5\r\n"
    assert out["result"]["stdout_truncated"] is False
    assert json.loads(route.calls.last.request.read()) == {"command": "hostname"}


@respx.mock
def test_guest_exec_truncates_large_output_and_says_so():
    huge = "x" * 10_000
    body = {"guest": 101, "stdout": huge, "stderr": "", "exitcode": 0}
    respx.post(f"{AGENT_HOSTCTL}/guest/101/shell").mock(return_value=httpx.Response(200, json=body))
    out = base.dispatch("guest_exec", {"guest": 101, "command": "cat bigfile"})
    result = out["result"]
    assert result["stdout_truncated"] is True
    assert result["stdout_total_chars"] == 10_000
    assert len(result["stdout"]) == infra._EXEC_OUTPUT_LIMIT


@respx.mock
def test_guest_exec_no_command_allowlist():
    """Any command - the owner removed ALLOWED_EXEC entirely."""
    body = {"guest": 101, "stdout": "", "stderr": "", "exitcode": 0}
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/shell").mock(
        return_value=httpx.Response(200, json=body)
    )
    out = base.dispatch("guest_exec", {"guest": 101, "command": "rm -rf /tmp/scratch"})
    assert out["ok"] is True
    assert route.call_count == 1


def test_guest_action_rejects_unlisted_action():
    out = base.dispatch("guest_action", {"guest": 105, "action": "destroy"})
    assert out["ok"] is False


def test_guest_action_rejects_stop_on_self_protected_guest():
    out = base.dispatch("guest_action", {"guest": 104, "action": "stop"})
    assert out["ok"] is False


def test_guest_action_rejects_reboot_on_docker_host_guest():
    out = base.dispatch("guest_action", {"guest": 101, "action": "reboot"})
    assert out["ok"] is False


@respx.mock
def test_guest_action_allows_start_on_self_protected_guest():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/104/action").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    out = base.dispatch("guest_action", {"guest": 104, "action": "start"})
    assert out["ok"] is True
    assert route.call_count == 1


@respx.mock
def test_zfs_report_calls_hostctl():
    respx.get(f"{AGENT_HOSTCTL}/zfs/status").mock(
        return_value=httpx.Response(200, json={"pool": "tank"})
    )
    assert base.dispatch("zfs_report", {}) == {"ok": True, "result": {"pool": "tank"}}


@respx.mock
def test_zfs_snapshot_forwards_dataset_and_label():
    route = respx.post(f"{AGENT_HOSTCTL}/zfs/snapshot").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    base.dispatch("zfs_snapshot", {"dataset": "tank/data", "label": "pre-upgrade"})
    assert json.loads(route.calls.last.request.read()) == {
        "dataset": "tank/data",
        "label": "pre-upgrade",
    }


@respx.mock
def test_host_metrics_calls_hostctl():
    respx.get(f"{AGENT_HOSTCTL}/host/metrics").mock(
        return_value=httpx.Response(200, json={"load": 0.5})
    )
    assert base.dispatch("host_metrics", {}) == {"ok": True, "result": {"load": 0.5}}


@respx.mock
def test_mt5_status_calls_hostctl():
    body = {"latest": {"equity": 685.14}, "latest_with_open_positions": None, "note": "x"}
    respx.get(f"{AGENT_HOSTCTL}/mt5/status").mock(return_value=httpx.Response(200, json=body))
    assert base.dispatch("mt5_status", {}) == {"ok": True, "result": body}


@respx.mock
def test_docker_stacks_execs_compose_ls():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/exec").mock(
        return_value=httpx.Response(200, json={"stdout": "[]"})
    )
    base.dispatch("docker_stacks", {})
    assert json.loads(route.calls.last.request.read()) == {
        "argv": ["docker", "compose", "ls", "--format", "json"]
    }


@respx.mock
def test_docker_action_forwards_stack_and_verb():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/exec").mock(
        return_value=httpx.Response(200, json={"stdout": ""})
    )
    base.dispatch("docker_action", {"stack": "uptime-kuma", "action": "up"})
    assert json.loads(route.calls.last.request.read()) == {
        "argv": [
            "docker",
            "compose",
            "-f",
            "/opt/stacks/uptime-kuma/compose.yaml",
            "up",
            "-d",
        ]
    }


def test_docker_action_rejects_unlisted_action():
    out = base.dispatch("docker_action", {"stack": "uptime-kuma", "action": "destroy"})
    assert out["ok"] is False


@respx.mock
def test_docker_action_rejects_path_traversal_in_stack():
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/exec").mock(
        return_value=httpx.Response(200, json={"stdout": ""})
    )
    out = base.dispatch("docker_action", {"stack": "../other-project/prod", "action": "up"})
    assert out["ok"] is False
    assert route.call_count == 0


@respx.mock
def test_zfs_snapshot_rejects_leading_dash_dataset():
    route = respx.post(f"{AGENT_HOSTCTL}/zfs/snapshot").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    out = base.dispatch("zfs_snapshot", {"dataset": "-r", "label": "pre-upgrade"})
    assert out["ok"] is False
    assert route.call_count == 0


@respx.mock
def test_zfs_snapshot_rejects_dataset_outside_tank():
    route = respx.post(f"{AGENT_HOSTCTL}/zfs/snapshot").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    out = base.dispatch("zfs_snapshot", {"dataset": "rpool/other", "label": "pre-upgrade"})
    assert out["ok"] is False
    assert route.call_count == 0


@respx.mock
def test_zfs_snapshot_accepts_nested_dataset_name():
    route = respx.post(f"{AGENT_HOSTCTL}/zfs/snapshot").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    out = base.dispatch("zfs_snapshot", {"dataset": "tank/dev/agent", "label": "pre-upgrade"})
    assert out["ok"] is True
    assert json.loads(route.calls.last.request.read()) == {
        "dataset": "tank/dev/agent",
        "label": "pre-upgrade",
    }


@respx.mock
def test_monitors_status_reports_empty_status_page_explicitly():
    """Regression test for the empty-heartbeat defect: Uptime Kuma answers
    200 with an empty heartbeatList for both a missing status page and a
    genuinely quiet homelab, so an empty result must not be returned as if
    it were meaningful data - the model needs to know the tool told it
    nothing, not that nothing is wrong."""
    respx.get("http://10.0.0.165:3001/api/status-page/heartbeat/homelab").mock(
        return_value=httpx.Response(200, json={"heartbeatList": {}, "uptimeList": {}})
    )
    out = base.dispatch("monitors_status", {})
    assert out["ok"] is True
    assert out["result"]["empty"] is True
    assert out["result"]["status_page_slug"] == "homelab"


@respx.mock
def test_monitors_status_uses_configured_slug(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_SLUG", "custom")
    route = respx.get("http://10.0.0.165:3001/api/status-page/heartbeat/custom").mock(
        return_value=httpx.Response(200, json={"heartbeatList": {"1": [{"status": 1}]}})
    )
    out = base.dispatch("monitors_status", {})
    assert route.call_count == 1
    assert out["result"]["heartbeatList"] == {"1": [{"status": 1}]}


@respx.mock
def test_monitors_status_includes_mt5():
    """Regression test for VM 200 parity: mt5 must be visible in
    monitors_status()'s response like any other monitor now - the owner
    wants the agent to see it everywhere, not have it scrubbed out."""
    respx.get("http://10.0.0.165:3001/api/status-page/heartbeat/homelab").mock(
        return_value=httpx.Response(
            200,
            json={
                "heartbeatList": {
                    "1": [{"monitor": "jellyfin", "status": 1}],
                    "mt5": [{"monitor": "mt5", "status": 1}],
                }
            },
        )
    )
    out = base.dispatch("monitors_status", {})
    assert "mt5" in out["result"]["heartbeatList"]
    assert "1" in out["result"]["heartbeatList"]


@respx.mock
def test_adguard_report_includes_mt5(monkeypatch):
    monkeypatch.setenv("ADGUARD_BASIC_AUTH", "dXNlcjpwYXNz")
    respx.get("http://10.0.0.165:8080/control/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "num_dns_queries": 1,
                "top_clients": [{"10.0.0.171": 5}, {"10.0.0.168": 3}],
            },
        )
    )
    out = base.dispatch("adguard_report", {})
    assert out["result"]["top_clients"] == [{"10.0.0.171": 5}, {"10.0.0.168": 3}]


@respx.mock
def test_adguard_report_sends_basic_auth(monkeypatch):
    monkeypatch.setenv("ADGUARD_BASIC_AUTH", "dXNlcjpwYXNz")
    route = respx.get("http://10.0.0.165:8080/control/stats").mock(
        return_value=httpx.Response(200, json={"num_dns_queries": 1})
    )
    out = base.dispatch("adguard_report", {})
    assert out == {"ok": True, "result": {"num_dns_queries": 1}}
    assert route.calls.last.request.headers["Authorization"] == "Basic dXNlcjpwYXNz"


@respx.mock
def test_host_exec_reaches_the_host_route():
    route = respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(
        return_value=httpx.Response(
            200, json={"stdout": "tech\n", "stderr": "", "exitcode": 0}
        )
    )
    out = base.dispatch("host_exec", {"command": "hostname"})
    assert out["ok"] is True
    assert out["result"]["stdout"] == "tech\n"
    assert out["result"]["stdout_truncated"] is False
    assert json.loads(route.calls.last.request.read()) == {"command": "hostname"}


@respx.mock
def test_host_exec_no_command_allowlist():
    """Any command - full root on the host, no allowlist, same as guest_exec."""
    respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(
        return_value=httpx.Response(200, json={"stdout": "", "stderr": "", "exitcode": 0})
    )
    out = base.dispatch("host_exec", {"command": "systemctl restart hostctl"})
    assert out["ok"] is True


@respx.mock
def test_host_exec_truncates_large_output_and_says_so():
    huge = "x" * 10_000
    respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(
        return_value=httpx.Response(
            200, json={"stdout": huge, "stderr": "", "exitcode": 0}
        )
    )
    out = base.dispatch("host_exec", {"command": "cat bigfile"})
    result = out["result"]
    assert result["stdout_truncated"] is True
    assert result["stdout_total_chars"] == 10_000
    assert len(result["stdout"]) == infra._EXEC_OUTPUT_LIMIT


@respx.mock
def test_host_exec_reports_timeout_as_failure():
    respx.post(f"{AGENT_HOSTCTL}/host/exec").mock(return_value=httpx.Response(504, json={}))
    out = base.dispatch("host_exec", {"command": "sleep 999999"})
    assert out["ok"] is False
    assert "504" in out["error"] or "Error" in out["error"]
