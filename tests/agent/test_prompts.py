"""Coverage for the VM 200 (mt5) system-prompt guardrails.

The owner gave the agent full administrative control over VM 200, then
explicitly and repeatedly removed the trading-specific prohibitions that
first replaced the old tool-level block: reading account state was always
allowed, and placing/modifying/closing trades and giving trading advice are
no longer restricted either. What's left is operational knowledge (what a
restart costs), not a restriction - these tests check the actual prompt
text for that, and check that no trading prohibition survived the reversal.
"""

from agent import tick
from agent.prompts import MT5_GUARDRAILS
from agent.slack_app import SYSTEM_CHAT


def test_restart_cost_is_stated_specifically():
    """The operational cost of a restart, from the owner's own notes:
    MetaTrader is force-killed, the profile doesn't save, only the
    startup-config EA reattaches, anything attached by hand is lost - and
    the agent must verify the result afterward. This is knowledge, not a
    restriction, and stays regardless of the trading-permission reversal."""
    assert "force-kill" in MT5_GUARDRAILS
    assert "profile" in MT5_GUARDRAILS and "not saved" in MT5_GUARDRAILS
    assert "startup-config" in MT5_GUARDRAILS or "startup EA" in MT5_GUARDRAILS
    assert "lost" in MT5_GUARDRAILS
    assert "verify" in MT5_GUARDRAILS.lower()
    assert "report" in MT5_GUARDRAILS.lower()


def test_reading_account_state_is_explicitly_encouraged():
    lowered = MT5_GUARDRAILS.lower()
    assert "positions" in lowered
    assert "balance" in lowered
    assert "encouraged" in lowered or "expected" in lowered


def test_no_trading_prohibition_survives():
    """The owner removed both the never-trade rule and the no-advice rule.
    Nothing in the prompt should still forbid placing, modifying, or
    closing a trade, or giving trading advice."""
    lowered = MT5_GUARDRAILS.lower()
    assert "never place" not in lowered
    assert "must never" not in lowered
    assert "trading advice" not in lowered
    assert "fais act" not in lowered
    assert "no signals" not in lowered
    assert "can't do that" not in lowered


def test_family_chat_prompt_includes_the_guardrails():
    assert MT5_GUARDRAILS in SYSTEM_CHAT


def test_daemon_prompt_includes_the_guardrails():
    assert MT5_GUARDRAILS in tick.SYSTEM_DAEMON


def test_neither_prompt_still_claims_mt5_is_out_of_scope():
    for prompt in (SYSTEM_CHAT, tick.SYSTEM_DAEMON):
        assert "out of scope" not in prompt
        assert "blocked at the tool level" not in prompt
        assert "no ability to start, stop" not in prompt
