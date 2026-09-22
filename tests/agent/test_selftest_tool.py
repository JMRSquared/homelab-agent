from agent import selftest
from agent.selftest import Probe, ProbeStatus
from agent.tools import base, selftest_tool  # noqa: F401


def test_self_test_tool_dispatches_and_returns_a_summary(monkeypatch):
    monkeypatch.setattr(
        selftest,
        "default_probes",
        lambda: [Probe("fake", ("some_tool",), lambda: (ProbeStatus.OK, "fine"))],
    )
    out = base.dispatch("self_test", {})
    assert out["ok"] is True
    assert out["result"]["summary"]["ok"] == 1
    assert out["result"]["results"][0]["probe"] == "fake"
