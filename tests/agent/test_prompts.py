"""Coverage for the VM 200 (mt5) system-prompt guardrails.

The owner gave the agent full administrative control over VM 200, removing
the old tool-level block. The hazard that replaces it is giving a model
destructive reach without telling it what the reach costs - so these tests
check the actual prompt text for the three required elements, not just that
some text exists.
"""

from agent import tick
from agent.prompts import MT5_GUARDRAILS
from agent.slack_app import SYSTEM_CHAT


def test_restart_cost_is_stated_specifically():
    """The operational cost of a restart, from the owner's own notes:
    MetaTrader is force-killed, the profile doesn't save, only the
    startup-config EA reattaches, anything attached by hand is lost - and
    the agent must verify the result afterward."""
    assert "force-kill" in MT5_GUARDRAILS
    assert "profile" in MT5_GUARDRAILS and "not saved" in MT5_GUARDRAILS
    assert "startup-config" in MT5_GUARDRAILS or "startup EA" in MT5_GUARDRAILS
    assert "lost" in MT5_GUARDRAILS
    assert "verify" in MT5_GUARDRAILS.lower()
    assert "report" in MT5_GUARDRAILS.lower()


def test_never_touches_a_trade():
    assert "never place, modify, or close a trade" in MT5_GUARDRAILS


def test_no_trading_advice_and_fais_act():
    assert "trading advice" in MT5_GUARDRAILS
    assert "FAIS Act" in MT5_GUARDRAILS


def test_family_chat_prompt_includes_the_guardrails():
    assert MT5_GUARDRAILS in SYSTEM_CHAT


def test_daemon_prompt_includes_the_guardrails():
    assert MT5_GUARDRAILS in tick.SYSTEM_DAEMON


def test_neither_prompt_still_claims_mt5_is_out_of_scope():
    for prompt in (SYSTEM_CHAT, tick.SYSTEM_DAEMON):
        assert "out of scope" not in prompt
        assert "blocked at the tool level" not in prompt
        assert "no ability to start, stop" not in prompt
