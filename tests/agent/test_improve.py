import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agent import config, improve
from agent.store import Store


def _settings(tmp_path: Any) -> config.Settings:
    return config.Settings(
        minimax_api_key="k",
        minimax_base_url="http://x/v1",
        model="MiniMax-M3",
        hostctl_url="http://h",
        hostctl_token="t",
        slack_bot_token="xoxb-fake",
        slack_app_token="xapp-fake",
        db_path=str(tmp_path / "d.db"),
        brain_path=str(tmp_path / "b.md"),
        tick_seconds=60,
    )


class FakeAgent:
    """Minimal stand-in for agent.model.Agent satisfying improve.Runner:
    run() plus set_audit(). Records every prompt/system it was asked to
    run and, when `audit_calls` is given, replays a scripted sequence of
    audit-callback texts before returning `answer` - just enough to
    exercise run_cycle's `posted_status` detection without a real model or
    real tool dispatch.
    """

    def __init__(
        self,
        answer: str,
        audit_texts: list[str] | None = None,
        raises: Exception | None = None,
    ):
        self.answer = answer
        self.audit_texts = audit_texts or []
        self.raises = raises
        self.runs: list[tuple[str, str, str]] = []
        self._audit: Callable[[str], Awaitable[None]] | None = None

    def set_audit(self, audit: Callable[[str], Awaitable[None]]) -> None:
        self._audit = audit

    async def run(
        self, prompt: str, *, priority: str, system: str, context: str | None = None
    ) -> str:
        self.runs.append((prompt, priority, system))
        if self.raises is not None:
            raise self.raises
        if self._audit is not None:
            for text in self.audit_texts:
                await self._audit(text)
        return self.answer


@pytest.fixture(autouse=True)
def _no_real_slack(monkeypatch):
    """Every test here drives run_cycle with a fake agent, but run_cycle
    still posts to Slack via agent.tools.comms.slack_say (audit mirror and
    the fallback report) - capture those calls instead of hitting the
    network."""
    posts: list[tuple[str, str]] = []

    def fake_slack_say(channel: str, text: str) -> dict[str, Any]:
        posts.append((channel, text))
        return {"ok": True}

    monkeypatch.setattr(improve.comms, "slack_say", fake_slack_say)
    return posts


def test_cycle_that_finds_nothing_stays_silent(tmp_path, monkeypatch, _no_real_slack):
    """A no-op cycle posts nothing at all.

    Originally this asserted a "nothing needs changing" report every cycle.
    The owner asked the agent in Slack on 2026-09-21 to stop doing that -
    144 such posts a day is noise - and the agent changed its own source to
    match (commit "agent/improve: only post a status report when there is
    something concrete to report"). The cycle still records the event to the
    store, so the run is auditable without being announced.
    """
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    monkeypatch.setattr(improve, "gather", _fake_gather)

    fake = FakeAgent(answer=improve.NOTHING_TOKEN)
    answer = asyncio.run(improve.run_cycle(settings, store, agent=fake))

    assert answer == improve.NOTHING_TOKEN
    assert _no_real_slack == [], "a cycle with nothing to report must stay silent"

def test_cycle_posts_fallback_when_model_forgets_to_report(tmp_path, monkeypatch, _no_real_slack):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    monkeypatch.setattr(improve, "gather", _fake_gather)

    fake = FakeAgent(answer="shipped a fix to the brain gap about the mail server")
    asyncio.run(improve.run_cycle(settings, store, agent=fake))

    posts = _no_real_slack
    assert len(posts) == 1
    channel, text = posts[0]
    assert channel == improve.slack_app.CH_HOMELAB
    assert "fallback" in text.lower()
    assert "mail server" in text


def test_cycle_skips_fallback_when_model_already_posted_status(
    tmp_path, monkeypatch, _no_real_slack
):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    monkeypatch.setattr(improve, "gather", _fake_gather)

    audit_text = (
        f'`slack_say` {{"channel": "{improve.slack_app.CH_HOMELAB}", "text": "done"}} -> ok'
    )
    fake = FakeAgent(answer="done", audit_texts=[audit_text])
    asyncio.run(improve.run_cycle(settings, store, agent=fake))

    posts = _no_real_slack
    # Only the audit mirror (to the log channel) fired - no fallback to the
    # status channel, since the model's own tool call already reached it.
    assert all(channel == improve.slack_app.CH_LOG for channel, _ in posts)


def test_cycle_records_an_event(tmp_path, monkeypatch, _no_real_slack):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    monkeypatch.setattr(improve, "gather", _fake_gather)

    fake = FakeAgent(answer="nothing needs changing")
    asyncio.run(improve.run_cycle(settings, store, agent=fake))

    events = improve._recent_events(settings.db_path, "improve_cycle", 5)
    assert len(events) == 1
    assert events[0]["answer"] == "nothing needs changing"
    assert events[0]["timed_out"] is False


def test_wall_clock_timeout_reports_and_does_not_raise(tmp_path, monkeypatch, _no_real_slack):
    settings = _settings(tmp_path)
    store = Store(settings.db_path)
    monkeypatch.setattr(improve, "gather", _fake_gather)
    monkeypatch.setattr(improve, "WALL_CLOCK_TIMEOUT_S", 0.05)

    class SlowAgent:
        def set_audit(self, audit):
            pass

        async def run(self, prompt, *, priority, system, context=None):
            await asyncio.sleep(10)
            return "too slow"

    answer = asyncio.run(improve.run_cycle(settings, store, agent=SlowAgent()))
    assert "wall-clock" in answer

    events = improve._recent_events(settings.db_path, "improve_cycle", 5)
    assert events[-1]["timed_out"] is True


def test_two_cycles_cannot_overlap(tmp_path):
    lock_path = str(tmp_path / "improve.lock")
    first = improve._acquire_lock(lock_path)
    assert first is not None
    try:
        second = improve._acquire_lock(lock_path)
        assert second is None
    finally:
        first.release()

    # Once released, a later cycle can take the lock again.
    third = improve._acquire_lock(lock_path)
    assert third is not None
    third.release()


async def _fake_gather(settings: config.Settings) -> dict[str, Any]:
    return {"homelab_state": {}, "note": "test context, gather() not exercised here"}
