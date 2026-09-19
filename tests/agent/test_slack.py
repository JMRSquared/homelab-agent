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


class FakeReactionsClient:
    """Records every reactions_add/reactions_remove call in order, so tests
    can assert the working-then-result sequence. `fail_on` names methods
    that should raise instead of succeeding, to prove a reaction failure
    never costs the user their reply."""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_on = fail_on

    async def reactions_add(self, **kwargs: Any) -> Any:
        self.calls.append(("add", kwargs))
        if "reactions_add" in self._fail_on:
            raise RuntimeError("already_reacted")

    async def reactions_remove(self, **kwargs: Any) -> Any:
        self.calls.append(("remove", kwargs))
        if "reactions_remove" in self._fail_on:
            raise RuntimeError("no_reaction")

    @property
    def reaction_names(self) -> list[tuple[str, str]]:
        """(method, emoji name) pairs, the shape most tests actually care about."""
        return [(method, kwargs["name"]) for method, kwargs in self.calls]


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
            client=FakeReactionsClient(),
            channel="C1",
            ts="1.1",
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
        slack_app.handle_message(
            agent=agent,
            text="how's it going",
            thread_ts="1.1",
            say=say,
            client=FakeReactionsClient(),
            channel="C1",
            ts="1.1",
        )
    )
    assert said[0]["text"] == "*status*: all good"


def test_mention_strips_the_bot_handle() -> None:
    agent = FakeAgent()

    async def say(**kwargs: str) -> None:
        return None

    asyncio.run(
        slack_app.handle_message(
            agent=agent,
            text="<@U123> hello",
            thread_ts="1.1",
            say=say,
            client=FakeReactionsClient(),
            channel="C1",
            ts="1.1",
        )
    )
    assert agent.calls[0][0] == "hello"


async def _noop_say(**kwargs: str) -> None:
    return None


def test_working_reaction_added_before_the_model_runs() -> None:
    """The whole point: the user sees feedback within moments of sending a
    message, well before a reply (which can take several seconds of tool
    calls) is ready."""
    order: list[str] = []

    class OrderTrackingAgent(FakeAgent):
        async def run(self, prompt: str, *, priority: str, system: str) -> str:
            order.append("agent.run")
            return await super().run(prompt, priority=priority, system=system)

    client = FakeReactionsClient()

    async def say(**kwargs: str) -> None:
        order.append("say")

    asyncio.run(
        slack_app.handle_message(
            agent=OrderTrackingAgent(),
            text="hi",
            thread_ts="1.1",
            say=say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names[0] == ("add", slack_app.REACTION_WORKING)
    assert order[0] == "agent.run"  # reaction landed before this, by construction


def test_working_reaction_removed_and_replaced_with_success() -> None:
    client = FakeReactionsClient()
    asyncio.run(
        slack_app.handle_message(
            agent=FakeAgent(),
            text="hi",
            thread_ts="1.1",
            say=_noop_say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names == [
        ("add", slack_app.REACTION_WORKING),
        ("remove", slack_app.REACTION_WORKING),
        ("add", slack_app.REACTION_SUCCESS),
    ]
    assert all(
        kwargs["channel"] == "C1" and kwargs["timestamp"] == "1.1" for _, kwargs in client.calls
    )


def test_working_reaction_removed_and_replaced_with_failure_when_model_raises() -> None:
    class BrokenAgent(FakeAgent):
        async def run(self, prompt: str, *, priority: str, system: str) -> str:
            raise RuntimeError("provider unreachable")

    client = FakeReactionsClient()
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=BrokenAgent(),
            text="hi",
            thread_ts="1.1",
            say=say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names == [
        ("add", slack_app.REACTION_WORKING),
        ("remove", slack_app.REACTION_WORKING),
        ("add", slack_app.REACTION_FAILURE),
    ]
    # A bare x with no explanation is not an answer - the user still gets a
    # message saying something went wrong.
    assert len(said) == 1
    assert said[0]["text"].strip() != ""


def test_failure_reaction_when_the_tool_loop_hits_its_round_cap() -> None:
    """Failure means the reply failed, not that a tool returned an error -
    the round-cap sentinel agent/model.py returns is a real failure (no
    answer was produced), distinct from the model successfully reporting a
    tool error like a missing API key."""

    class RoundCapAgent(FakeAgent):
        async def run(self, prompt: str, *, priority: str, system: str) -> str:
            return "stopped: exceeded the tool-call round limit"

    client = FakeReactionsClient()
    asyncio.run(
        slack_app.handle_message(
            agent=RoundCapAgent(),
            text="hi",
            thread_ts="1.1",
            say=_noop_say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names[-1] == ("add", slack_app.REACTION_FAILURE)


def test_tool_error_reported_in_the_answer_is_still_a_success() -> None:
    """The agent successfully answering "the AdGuard key is missing" is a
    successful reply, not a failure - reserve the x reaction for the reply
    itself failing, not for a tool inside it returning an error."""

    class ToolErrorAgent(FakeAgent):
        async def run(self, prompt: str, *, priority: str, system: str) -> str:
            return "I couldn't check AdGuard: the API key isn't configured."

    client = FakeReactionsClient()
    asyncio.run(
        slack_app.handle_message(
            agent=ToolErrorAgent(),
            text="is adguard up?",
            thread_ts="1.1",
            say=_noop_say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names[-1] == ("add", slack_app.REACTION_SUCCESS)


def test_reply_still_posts_when_reactions_add_itself_throws() -> None:
    """Reactions must never break a reply - a decoration that can swallow
    the actual work is worse than no decoration."""
    client = FakeReactionsClient(fail_on=frozenset({"reactions_add"}))
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=FakeAgent(),
            text="hi",
            thread_ts="1.1",
            say=say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert len(said) == 1
    assert "answered" in said[0]["text"]


def test_reply_still_posts_when_reactions_remove_itself_throws() -> None:
    client = FakeReactionsClient(fail_on=frozenset({"reactions_remove"}))
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=FakeAgent(),
            text="hi",
            thread_ts="1.1",
            say=say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert len(said) == 1
    # The result reaction still gets attempted even though remove() failed.
    assert client.reaction_names[-1] == ("add", slack_app.REACTION_SUCCESS)


def test_working_reaction_still_removed_when_the_reply_fails_to_post() -> None:
    """try/finally, not a bare sequence: the working emoji must come off
    even when say() itself raises, or a broken thread is left with a
    permanent hourglass."""

    async def broken_say(**kwargs: str) -> None:
        raise RuntimeError("channel_not_found")

    client = FakeReactionsClient()
    asyncio.run(
        slack_app.handle_message(
            agent=FakeAgent(),
            text="hi",
            thread_ts="1.1",
            say=broken_say,
            client=client,
            channel="C1",
            ts="1.1",
        )
    )
    assert client.reaction_names == [
        ("add", slack_app.REACTION_WORKING),
        ("remove", slack_app.REACTION_WORKING),
        ("add", slack_app.REACTION_FAILURE),
    ]


def test_dm_gets_reactions_too() -> None:
    """The feedback is just as useful in a DM as in a channel - nothing in
    the DM path should lack the channel id or message ts."""
    agent = FakeAgent()
    said, client, _ = _run_message_event(
        agent,
        {"channel_type": "im", "channel": "D1", "user": "U999", "text": "hello", "ts": "1.1"},
    )
    assert len(said) == 1
    assert client.reaction_names == [
        ("add", slack_app.REACTION_WORKING),
        ("remove", slack_app.REACTION_WORKING),
        ("add", slack_app.REACTION_SUCCESS),
    ]
    assert all(kwargs["channel"] == "D1" for _, kwargs in client.calls)


def test_reaction_uses_the_message_ts_not_the_thread_parent_ts() -> None:
    """Reacting to the thread parent instead of the message they just sent
    would be wrong - event["ts"] (this message) and thread_ts (the
    parent, when this is a threaded reply) can differ."""
    agent = FakeAgent()
    said, client, _ = _run_message_event(
        agent,
        {
            "channel_type": "channel",
            "channel": "C1",
            "user": "U999",
            "text": "is jellyfin up?",
            "ts": "2.2",
            "thread_ts": "1.1",
        },
    )
    assert said[0]["thread_ts"] == "1.1"
    assert all(kwargs["timestamp"] == "2.2" for _, kwargs in client.calls)


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
) -> tuple[list[dict[str, str]], FakeReactionsClient, object]:
    store = Store(":memory:")
    app = slack_app.build(agent, store, bot_token="xoxb-test")
    listener = _message_listener(app)

    said: list[dict[str, str]] = []
    client = FakeReactionsClient()

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(listener(event=event, say=say, client=client))
    return said, client, store


@pytest.fixture(autouse=True)
def _clear_mention_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_REPLY_WITHOUT_MENTION", raising=False)


def test_plain_channel_message_reaches_the_agent_by_default() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "channel",
            "channel": "C1",
            "user": "U999",
            "text": "is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert said[0]["thread_ts"] == "1.1"


def test_channel_message_with_mention_reaches_agent_exactly_once_mention_stripped() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "channel",
            "channel": "C1",
            "user": "U999",
            "text": "<@U123> is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_private_channel_message_reaches_the_agent_by_default() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "group",
            "channel": "C1",
            "user": "U999",
            "text": "is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_dm_still_works() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {"channel_type": "im", "channel": "D1", "user": "U999", "text": "hello", "ts": "1.1"},
    )
    assert agent.calls == [("hello", "family")]
    assert len(said) == 1


def test_bot_own_message_ignored_on_message_path() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {"channel_type": "channel", "bot_id": "B1", "text": "I did a thing", "ts": "1.1"},
    )
    assert agent.calls == []
    assert said == []


def test_message_changed_ignored_on_message_path() -> None:
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
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
    said, _client, _ = _run_message_event(
        agent, {"channel_type": "im", "subtype": "message_deleted", "ts": "1.1"}
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_ignores_plain_channel_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "channel",
            "channel": "C1",
            "user": "U999",
            "text": "is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_still_answers_a_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "channel",
            "channel": "C1",
            "user": "U999",
            "text": "<@U123> is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == [("is jellyfin up?", "family")]
    assert len(said) == 1


def test_mention_required_mode_ignores_plain_private_channel_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent,
        {
            "channel_type": "group",
            "channel": "C1",
            "user": "U999",
            "text": "is jellyfin up?",
            "ts": "1.1",
        },
    )
    assert agent.calls == []
    assert said == []


def test_mention_required_mode_still_answers_a_dm_without_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_REPLY_WITHOUT_MENTION", "0")
    agent = FakeAgent()
    said, _client, _ = _run_message_event(
        agent, {"channel_type": "im", "channel": "D1", "user": "U999", "text": "hello", "ts": "1.1"}
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
