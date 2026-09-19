import asyncio
import json

import httpx
import pytest
import respx

from agent import slack_app
from agent.store import Store
from agent.tools import comms


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, priority: str, system: str) -> str:
        self.calls.append((prompt, priority))
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


def test_should_ignore_rejects_non_im_channel_type() -> None:
    assert slack_app.should_ignore({"channel_type": "channel", "text": "hi"})


def test_should_ignore_allows_plain_human_dm() -> None:
    assert not slack_app.should_ignore({"channel_type": "im", "user": "U999", "text": "hi"})


def _dm_listener(app: object) -> object:
    """Pull out the exact function `build()` registered for the `message` event.

    Calling it directly (rather than driving Bolt's full dispatch machinery)
    exercises the real guard-then-handle_message code path production runs,
    without needing a live Slack request/signature to satisfy Bolt's request
    matchers.
    """
    for listener in app._async_listeners:  # type: ignore[attr-defined]
        if listener.ack_function.__name__ == "_dm":
            return listener.ack_function
    raise AssertionError("no _dm listener registered on the message event")


def test_message_changed_in_dm_does_not_reach_the_agent() -> None:
    agent = FakeAgent()
    store = Store(":memory:")
    app = slack_app.build(agent, store, bot_token="xoxb-test")
    dm_handler = _dm_listener(app)

    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        dm_handler(
            event={
                "subtype": "message_changed",
                "channel_type": "im",
                "message": {"text": "edited"},
                "ts": "1.1",
            },
            say=say,
        )
    )
    assert agent.calls == []
    assert said == []


def test_message_deleted_in_dm_does_not_reach_the_agent() -> None:
    agent = FakeAgent()
    store = Store(":memory:")
    app = slack_app.build(agent, store, bot_token="xoxb-test")
    dm_handler = _dm_listener(app)

    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        dm_handler(
            event={"subtype": "message_deleted", "channel_type": "im", "ts": "1.1"},
            say=say,
        )
    )
    assert agent.calls == []
    assert said == []


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
    assert sent == {"channel": "#homelab", "text": "hello"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer xoxb-test"
