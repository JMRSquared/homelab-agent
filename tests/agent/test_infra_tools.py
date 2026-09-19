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
    route = respx.post(f"{AGENT_HOSTCTL}/guest/101/action").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )
    base.dispatch("guest_action", {"guest": 101, "action": "reboot"})
    assert json.loads(route.calls.last.request.read()) == {"action": "reboot"}


def test_guest_action_rejects_guest_200():
    out = base.dispatch("guest_action", {"guest": 200, "action": "stop"})
    assert out["ok"] is False


def test_guest_action_rejects_unlisted_action():
    out = base.dispatch("guest_action", {"guest": 101, "action": "destroy"})
    assert out["ok"] is False


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
def test_monitors_status_calls_uptime_kuma():
    respx.get("http://10.0.0.165:3001/api/status-page/heartbeat/homelab").mock(
        return_value=httpx.Response(200, json={"heartbeatList": {}})
    )
    assert base.dispatch("monitors_status", {}) == {
        "ok": True,
        "result": {"heartbeatList": {}},
    }


@respx.mock
def test_adguard_report_sends_basic_auth(monkeypatch):
    monkeypatch.setenv("ADGUARD_BASIC_AUTH", "dXNlcjpwYXNz")
    route = respx.get("http://10.0.0.165:8080/control/stats").mock(
        return_value=httpx.Response(200, json={"num_dns_queries": 1})
    )
    out = base.dispatch("adguard_report", {})
    assert out == {"ok": True, "result": {"num_dns_queries": 1}}
    assert route.calls.last.request.headers["Authorization"] == "Basic dXNlcjpwYXNz"
