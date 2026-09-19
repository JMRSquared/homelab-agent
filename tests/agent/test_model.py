import asyncio
import json

import pytest
from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message_custom_tool_call import (
    ChatCompletionMessageCustomToolCall,
    Custom,
)
from openai.types.chat.chat_completion_message_function_tool_call import Function

from agent.model import MAX_TOOL_ROUNDS, Agent
from agent.tools import base


def _settings(tmp_path):
    from agent.config import Settings

    return Settings(
        minimax_api_key="k", minimax_base_url="http://x/v1", model="MiniMax-M3",
        hostctl_url="http://h", hostctl_token="t", slack_bot_token="b",
        slack_app_token="a", db_path=str(tmp_path / "d.db"), brain_path=str(tmp_path / "b.md"),
    )


def _completion(message: ChatCompletionMessage) -> ChatCompletion:
    return ChatCompletion(
        id="resp_1",
        choices=[Choice(finish_reason="tool_calls", index=0, message=message)],
        created=0,
        model="MiniMax-M3",
        object="chat.completion",
    )


def test_family_priority_preempts_daemon(monkeypatch, tmp_path):
    from agent.config import Settings
    from agent.store import Store

    settings = Settings(
        minimax_api_key="k", minimax_base_url="http://x/v1", model="MiniMax-M3",
        hostctl_url="http://h", hostctl_token="t", slack_bot_token="b",
        slack_app_token="a", db_path=str(tmp_path / "d.db"), brain_path=str(tmp_path / "b.md"),
    )
    agent = Agent(settings, Store(settings.db_path))
    order: list[str] = []

    async def fake_call(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "done"

    monkeypatch.setattr(agent, "_complete", fake_call)

    async def scenario():
        daemons = [
            asyncio.create_task(agent.run("d", priority="daemon", system="s"))
            for _ in range(4)
        ]
        await asyncio.sleep(0.01)
        fam = asyncio.create_task(agent.run("f", priority="family", system="s"))
        await fam
        order.append("family")
        await asyncio.gather(*daemons)
        order.append("daemons")

    asyncio.run(scenario())
    assert order == ["family", "daemons"]


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(base.REGISTRY)
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(snapshot)


def test_concurrent_tool_calls_do_not_serialize(monkeypatch, tmp_path):
    """Regression test for I1: dispatch() used to run bare inside _complete,
    so a blocking tool call held the event loop and stalled every other
    concurrency slot for its whole duration. With dispatch() offloaded to a
    thread, two concurrent Agent.run calls whose tools each block for 0.2s
    must overlap rather than serialize."""
    import time

    from agent.store import Store

    events: list[tuple[str, float]] = []

    @base.tool("slow", "blocks the calling thread", {"type": "object", "properties": {}})
    def _slow() -> dict:
        events.append(("start", time.monotonic()))
        time.sleep(0.2)
        events.append(("end", time.monotonic()))
        return {}

    settings = _settings(tmp_path)
    agent = Agent(settings, Store(settings.db_path))

    async def fake_create(*_args, **kwargs):
        already_has_tool_result = any(m.get("role") == "tool" for m in kwargs["messages"])
        if already_has_tool_result:
            return _completion(ChatCompletionMessage(role="assistant", content="done"))
        tool_call = ChatCompletionMessageFunctionToolCall(
            id="call_1", type="function", function=Function(name="slow", arguments="{}")
        )
        return _completion(
            ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool_call])
        )

    monkeypatch.setattr(agent._client.chat.completions, "create", fake_create)

    async def scenario():
        await asyncio.gather(
            agent.run("a", priority="family", system="s"),
            agent.run("b", priority="family", system="s"),
        )

    asyncio.run(scenario())

    starts = sorted(t for kind, t in events if kind == "start")
    ends = sorted(t for kind, t in events if kind == "end")
    assert len(starts) == 2 and len(ends) == 2
    # Serialized, the second call couldn't start until the first finished
    # sleeping. Run concurrently, the second start lands before the first end.
    assert starts[1] < ends[0]


def test_max_tool_rounds_caps_the_loop(monkeypatch, tmp_path):
    from agent.store import Store

    @base.tool("noop", "does nothing", {"type": "object", "properties": {}})
    def _noop() -> dict:
        return {}

    agent = Agent(_settings(tmp_path), Store(_settings(tmp_path).db_path))
    call_count = 0

    async def fake_create(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        tool_call = ChatCompletionMessageFunctionToolCall(
            id=f"call_{call_count}",
            type="function",
            function=Function(name="noop", arguments="{}"),
        )
        message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool_call])
        return _completion(message)

    monkeypatch.setattr(agent._client.chat.completions, "create", fake_create)

    result = asyncio.run(agent.run("go", priority="family", system="s"))

    assert result == "stopped: exceeded the tool-call round limit"
    assert call_count == MAX_TOOL_ROUNDS


def test_unsupported_tool_call_type_becomes_typed_error(monkeypatch, tmp_path):
    from agent.store import Store

    agent = Agent(_settings(tmp_path), Store(_settings(tmp_path).db_path))
    calls = 0
    seen_messages: list[list[dict]] = []

    async def fake_create(*_args, **kwargs):
        nonlocal calls
        calls += 1
        seen_messages.append(kwargs["messages"])
        if calls == 1:
            custom_call = ChatCompletionMessageCustomToolCall(
                id="call_1", type="custom", custom=Custom(name="weird", input="x")
            )
            message = ChatCompletionMessage(
                role="assistant", content=None, tool_calls=[custom_call]
            )
        else:
            message = ChatCompletionMessage(role="assistant", content="done")
        return _completion(message)

    monkeypatch.setattr(agent._client.chat.completions, "create", fake_create)

    result = asyncio.run(agent.run("go", priority="family", system="s"))

    assert result == "done"
    assert calls == 2

    # The second call's message history must carry the typed error for the
    # unsupported tool-call type, not a raised exception or a silent skip.
    tool_reply = seen_messages[1][-1]
    assert tool_reply["role"] == "tool"
    assert tool_reply["tool_call_id"] == "call_1"
    payload = json.loads(tool_reply["content"])
    assert payload["ok"] is False
    assert "ChatCompletionMessageCustomToolCall" in payload["error"]


def test_audit_callback_receives_every_tool_call(monkeypatch, tmp_path):
    from agent.store import Store

    @base.tool("noop", "does nothing", {"type": "object", "properties": {}})
    def _noop() -> dict:
        return {}

    settings = _settings(tmp_path)
    agent = Agent(settings, Store(settings.db_path))
    posted: list[tuple[str, str]] = []

    async def audit(channel: str, text: str) -> None:
        posted.append((channel, text))

    agent.set_audit(audit)

    calls = 0

    async def fake_create(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_call = ChatCompletionMessageFunctionToolCall(
                id="call_1", type="function", function=Function(name="noop", arguments="{}")
            )
            message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool_call])
        else:
            message = ChatCompletionMessage(role="assistant", content="done")
        return _completion(message)

    monkeypatch.setattr(agent._client.chat.completions, "create", fake_create)

    result = asyncio.run(agent.run("go", priority="family", system="s"))

    assert result == "done"
    assert posted == [("#agent-log", '`noop` {} -> ok')]


def test_audit_callback_failure_does_not_break_the_tool_loop(monkeypatch, tmp_path):
    from agent.store import Store

    @base.tool("noop", "does nothing", {"type": "object", "properties": {}})
    def _noop() -> dict:
        return {}

    settings = _settings(tmp_path)
    agent = Agent(settings, Store(settings.db_path))

    async def failing_audit(_channel: str, _text: str) -> None:
        raise RuntimeError("slack is down")

    agent.set_audit(failing_audit)

    calls = 0

    async def fake_create(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_call = ChatCompletionMessageFunctionToolCall(
                id="call_1", type="function", function=Function(name="noop", arguments="{}")
            )
            message = ChatCompletionMessage(role="assistant", content=None, tool_calls=[tool_call])
        else:
            message = ChatCompletionMessage(role="assistant", content="done")
        return _completion(message)

    monkeypatch.setattr(agent._client.chat.completions, "create", fake_create)

    result = asyncio.run(agent.run("go", priority="family", system="s"))

    assert result == "done"
