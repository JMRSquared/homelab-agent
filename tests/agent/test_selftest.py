import httpx
import pytest
import respx

from agent import selftest
from agent.selftest import Probe, ProbeStatus, Unavailable, run_probe, run_self_test

AGENT_HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", AGENT_HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")
    monkeypatch.delenv("ADGUARD_BASIC_AUTH", raising=False)


def test_ok_probe_reports_ok():
    probe = Probe("thing", ("some_tool",), lambda: (ProbeStatus.OK, "answered with data"))
    result = run_probe(probe)
    assert result.status == ProbeStatus.OK
    assert result.tools == ("some_tool",)


def test_empty_answer_is_reported_as_suspicious_not_healthy():
    """Regression for the exact Uptime Kuma failure this suite exists to
    catch: a tool that answers with no data must not be reported the same
    as a tool that answered with real data."""
    probe = Probe(
        "uptime_kuma", ("monitors_status",), lambda: (ProbeStatus.EMPTY, "no monitors")
    )
    result = run_probe(probe)
    assert result.status == ProbeStatus.EMPTY
    assert result.status != ProbeStatus.OK


def test_probe_exception_becomes_failed_not_a_crash():
    def _boom():
        raise RuntimeError("connection refused")

    probe = Probe("thing", ("some_tool",), _boom)
    result = run_probe(probe)
    assert result.status == ProbeStatus.FAILED
    assert "connection refused" in result.detail


def test_unavailable_is_a_distinct_state_from_failed():
    def _missing_config():
        raise Unavailable("ADGUARD_BASIC_AUTH is not set")

    probe = Probe("adguard", ("adguard_report",), _missing_config)
    result = run_probe(probe)
    assert result.status == ProbeStatus.UNAVAILABLE
    assert result.status != ProbeStatus.FAILED


def test_run_self_test_aggregates_summary_counts():
    probes = [
        Probe("a", ("t1",), lambda: (ProbeStatus.OK, "fine")),
        Probe("b", ("t2",), lambda: (ProbeStatus.EMPTY, "empty")),
        Probe("c", ("t3",), lambda: (_ for _ in ()).throw(RuntimeError("down"))),
        Probe("d", ("t4",), lambda: (_ for _ in ()).throw(Unavailable("no key"))),
    ]
    report = run_self_test(probes)
    assert report["summary"] == {"ok": 1, "empty": 1, "failed": 1, "unavailable": 1}
    assert len(report["results"]) == 4
    assert {r["probe"] for r in report["results"]} == {"a", "b", "c", "d"}


def test_run_self_test_with_no_probes_is_a_no_op():
    report = run_self_test([])
    assert report["summary"] == {"ok": 0, "empty": 0, "failed": 0, "unavailable": 0}
    assert report["results"] == []


def test_default_probes_cover_every_externally_reaching_tool():
    """Every one of the 32 tools that talk to something external must be
    covered by some probe's `tools` tuple - a tool nobody probes is exactly
    the gap this suite exists to close.

    Deliberately a fixed list, not `base.REGISTRY` - the registry is
    process-wide shared state that other concurrently-developed tool
    modules (owned elsewhere in this codebase) also populate, and this
    suite's coverage obligation is scoped to the 32 tools named in the
    observability brief, not to whatever else happens to be registered in
    the same test run.
    """
    externally_reaching = {
        "guests_list", "guest_action", "guest_exec", "host_exec",
        "zfs_report", "zfs_snapshot", "host_metrics", "mt5_status",
        "mt5_screenshot", "docker_stacks", "docker_action",
        "monitors_status", "adguard_report", "media_search", "media_request",
        "media_last_watched", "media_library_status", "photos_search",
        "photos_download", "photos_stats", "mail_list_messages",
        "mail_read_message", "send_email", "slack_say", "slack_history",
        "slack_thread_replies", "image_inspect", "self_deploy",
        "hostctl_restart_verified",
    }

    covered: set[str] = set()
    for probe in selftest.default_probes():
        covered.update(probe.tools)

    assert externally_reaching <= covered, f"uncovered tools: {externally_reaching - covered}"


@respx.mock
def test_real_hostctl_probe_reports_empty_on_an_empty_guest_list():
    """Same class of failure as the Uptime Kuma incident this suite exists
    to catch, but against the real hostctl-backed probe rather than a
    synthetic one."""
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(return_value=httpx.Response(200, json={"guests": []}))
    probes = [p for p in selftest.default_probes() if p.name == "hostctl"]
    result = run_probe(probes[0])
    assert result.status == ProbeStatus.EMPTY


@respx.mock
def test_real_hostctl_probe_reports_ok_with_data():
    respx.get(f"{AGENT_HOSTCTL}/guests").mock(
        return_value=httpx.Response(200, json={"guests": [{"id": 101, "status": "running"}]})
    )
    probes = [p for p in selftest.default_probes() if p.name == "hostctl"]
    result = run_probe(probes[0])
    assert result.status == ProbeStatus.OK


def test_real_adguard_probe_is_unavailable_without_the_env_var():
    probes = [p for p in selftest.default_probes() if p.name == "adguard"]
    result = run_probe(probes[0])
    assert result.status == ProbeStatus.UNAVAILABLE
