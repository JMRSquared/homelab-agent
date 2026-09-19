import asyncio
import importlib
import json
from typing import Any

import httpx
import pytest
import respx

from agent import slack_app
from agent.store import Store
from agent.tools import comms


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.systems: list[str] = []

    async def run(self, prompt: str, *, priority: str, system: str) -> str:
        self.calls.append((prompt, priority))
        self.systems.append(system)
        return f"answered: {prompt}"


def test_mention_runs_with_family_priority() -> None:
    agent = FakeAgent()
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=agent,
            text="<@U123> is jellyfin up?",
            thread_ts="1.1",
            say=say,
        )
    )
    assert agent.calls[0][1] == "family"
    assert said[0]["thread_ts"] == "1.1"
    assert "answered" in said[0]["text"]
    assert agent.systems[0] == slack_app.SYSTEM_CHAT


def test_handle_message_posts_slack_mrkdwn_not_raw_markdown() -> None:
    """agent.run()'s Markdown reply must be converted to Slack mrkdwn at the
    point handle_message posts it - the model itself, and the caller here
    (FakeAgent), keep producing plain Markdown; only what actually reaches
    `say` should be transformed."""

    class MarkdownAgent(FakeAgent):
        async def run(self, prompt: str, *, priority: str, system: str) -> str:
            await super().run(prompt, priority=priority, system=system)
            return "**status**: all good"

    agent = MarkdownAgent()
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(agent=agent, text="how's it going", thread_ts="1.1", say=say)
    )
    assert said[0]["text"] == "*status*: all good"


def test_mention_strips_the_bot_handle() -> None:
    agent = FakeAgent()

    async def say(**kwargs: str) -> None:
        return None

    asyncio.run(
        slack_app.handle_message(agent=agent, text="<@U123> hello", thread_ts="1.1", say=say)
    )
    assert agent.calls[0][0] == "hello"


def test_build_returns_an_async_app() -> None:
    agent = FakeAgent()
    store = Store(":memory:")
    app = slack_app.build(agent, store, bot_token="xoxb-test")
    assert app is not None


def test_should_ignore_rejects_bot_id() -> None:
    assert slack_app.should_ignore({"channel_type": "im", "bot_id": "B123", "text": "hi"})


def test_should_ignore_rejects_bot_message_subtype() -> None:
    assert slack_app.should_ignore(
        {"channel_type": "im", "subtype": "bot_message", "text": "hi"}
    )


def test_should_ignore_rejects_message_changed_subtype() -> None:
    assert slack_app.should_ignore(
        {"channel_type": "im", "subtype": "message_changed", "message": {"text": "hi"}}
    )


def test_should_ignore_rejects_message_deleted_subtype() -> None:
    assert slack_app.should_ignore({"channel_type": "im", "subtype": "message_deleted"})


def test_should_ignore_allows_plain_human_dm() -> None:
    assert not slack_app.should_ignore({"channel_type": "im", "user": "U999", "text": "hi"})


def test_should_ignore_allows_plain_channel_message() -> None:
    assert not slack_app.should_ignore({"channel_type": "channel", "user": "U999", "text": "hi"})


def test_should_ignore_allows_plain_private_channel_message() -> None:
    assert not slack_app.should_ignore({"channel_type": "group", "user": "U999", "text": "hi"})


def _message_listener(app: object) -> object:
    """Pull out the exact function `build()` registered for the `message` event.

    Calling it directly (rather than driving Bolt's full dispatch machinery)
    exercises the real guard-then-handle_message code path production runs,
    without needing a live Slack request/signature to satisfy Bolt's request
    matchers.
    """
    for listener in app._async_listeners:  # type: ignore[attr-defined]
        if listener.ack_function.__name__ == "_message":
            return listener.ack_function
    raise AssertionError("no _message listener registered on the message event")


def _run_message_event(
    agent: FakeAgent, event: dict[str, object]
) -> tuple[list[dict[str, str]], object]:
    store = Store(":memory:")
    app = slack_app.build(agent, store, bot_token="xoxb-test")
    listener = _message_listener(app)

    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(listener(event=event, say=say))
    return said, store


@pytest.fixture(autouse=True)
def _clear_mention_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_REPLY_WITHOUT_MENTION", raising=False)


def test_plain_channel_message_reaches_the_agent_by_default() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "user": "U999", "text": "is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert said[0]["thread_ts"] == "1.1"


def test_channel_message_with_mention_reaches_agent_exactly_once_mention_stripped() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "user": "U999", "text": "<@U123> is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_private_channel_message_reaches_the_agent_by_default() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "group", "user": "U999", "text": "is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_dm_still_works() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "im", "user": "U999", "text": "hello", "ts": "1.1"},
    )
    assert agent.calls == [("hello", "family")]
    assert len(said) == 1


def test_bot_own_message_ignored_on_message_path() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "bot_id": "B1", "text": "I did a thing", "ts": "1.1"},
    )
    assert agent.calls == []
    assert said == []


def test_message_changed_ignored_on_message_path() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {
            "channel_type": "im",
            "subtype": "message_changed",
            "message": {"text": "edited"},
            "ts": "1.1",
        },
    )
    assert agent.calls == []
    assert said == []


def test_message_deleted_ignored_on_message_path() -> None:
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent, {"channel_type": "im", "subtype": "message_deleted", "ts": "1.1"}
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_ignores_plain_channel_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "user": "U999", "text": "is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_still_answers_a_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "user": "U999", "text": "<@U123> is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_mention_required_mode_ignores_plain_private_channel_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent,
        {"channel_type": "group", "user": "U999", "text": "is jellyfin up?", "ts": "1.1"},
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_still_answers_a_dm_without_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _ = _run_message_event(
        agent, {"channel_type": "im", "user": "U999", "text": "hello", "ts": "1.1"}
    )
    assert agent.calls == [("hello", "family")]
    assert len(said) == 1


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


@respx.mock
def test_slack_say_posts_to_chat_post_message() -> None:
    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    out = comms.slack_say(channel=slack_app.CH_HOMELAB, text="hello")
    assert out == {"ok": True}
    sent = json.loads(route.calls.last.request.read())
    assert sent == {"channel": slack_app.CH_HOMELAB, "text": "hello"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer xoxb-test"


@respx.mock
def test_slack_say_converts_markdown_to_mrkdwn_before_posting() -> None:
    route = respx.post("https://slack.com/api/chat.postMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    comms.slack_say(channel=slack_app.CH_HOMELAB, text="**wrench**: restarted `jellyfin`")
    sent = json.loads(route.calls.last.request.read())
    assert sent["text"] == "*wrench*: restarted `jellyfin`"


def test_default_channel_constants() -> None:
    assert slack_app.CH_HOMELAB == "#homelab-alerts"
    assert slack_app.CH_LOG == "#homelab-agent-log"


def test_channel_env_vars_override_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_CHANNEL_STATUS", "#ops")
    monkeypatch.setenv("SLACK_CHANNEL_LOG", "#audit")
    importlib.reload(slack_app)
    try:
        assert slack_app.CH_HOMELAB == "#ops"
        assert slack_app.CH_LOG == "#audit"
    finally:
        # Restore the module to its default state immediately, rather than
        # relying on monkeypatch's teardown timing, so every later test in
        # this file still sees the real defaults.
        monkeypatch.delenv("SLACK_CHANNEL_STATUS", raising=False)
        monkeypatch.delenv("SLACK_CHANNEL_LOG", raising=False)
        importlib.reload(slack_app)


class FakeConversationsClient:
    def __init__(self, channel_names: list[str]) -> None:
        self._channel_names = channel_names
        self.calls: list[dict[str, Any]] = []

    async def users_conversations(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"channels": [{"name": name} for name in self._channel_names]}


class FailingConversationsClient:
    async def users_conversations(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("missing_scope")


def test_preflight_channels_reports_nothing_missing_when_bot_is_in_both() -> None:
    client = FakeConversationsClient(["homelab-alerts", "homelab-agent-log"])
    missing = asyncio.run(slack_app.preflight_channels(client))
    assert missing == []


def test_preflight_channels_reports_the_missing_one(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeConversationsClient(["homelab-alerts"])
    with caplog.at_level("WARNING"):
        missing = asyncio.run(slack_app.preflight_channels(client))
    assert missing == ["SLACK_CHANNEL_LOG"]
    assert any(
        "homelab-agent-log" in record.message and "not found or bot not a member" in record.message
        for record in caplog.records
    )


def test_preflight_channels_reports_both_missing_when_it_is_in_neither() -> None:
    client = FakeConversationsClient([])
    missing = asyncio.run(slack_app.preflight_channels(client))
    assert set(missing) == {"SLACK_CHANNEL_STATUS", "SLACK_CHANNEL_LOG"}


def test_preflight_channels_never_raises_on_a_client_error() -> None:
    missing = asyncio.run(slack_app.preflight_channels(FailingConversationsClient()))
    assert set(missing) == {"SLACK_CHANNEL_STATUS", "SLACK_CHANNEL_LOG"}
