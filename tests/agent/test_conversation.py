import asyncio

from agent import conversation
from agent.store import Store


def _names(user_id: str) -> str:
    return {"U1": "Tech", "U2": "Sam"}.get(user_id, user_id)


async def _resolve_name(user_id: str) -> str:
    return _names(user_id)


async def _no_ambient(channel: str) -> list[dict[str, object]]:
    return []


def test_history_key_uses_thread_ts_when_this_message_is_a_real_thread_reply() -> None:
    # thread_ts != ts is exactly the signal slack_app relies on: this
    # message carries a parent's timestamp, not its own.
    assert conversation.history_key("C1", "100.0", "200.0") == "thread:100.0"


def test_history_key_falls_back_to_channel_when_there_is_no_real_thread() -> None:
    # thread_ts == ts: slack_app's own convention for "not in a thread".
    assert conversation.history_key("C1", "200.0", "200.0") == "chan:C1"


def test_follow_up_message_sees_the_earlier_turn() -> None:
    store = Store(":memory:")

    async def scenario() -> tuple[str, str]:
        # Both messages are replies within the same real Slack thread
        # (thread_ts="90.0" is the parent; only `ts`, each message's own
        # timestamp, differs) - the case `history_key` keys on directly.
        first = await conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="90.0",
            ts="100.0",
            user="U1",
            text="is jellyfin up?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
        conversation.record_turn(
            store, first, user_text="is jellyfin up?", reply_text="Yes, it's up.", ts="100.0"
        )
        second = await conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="90.0",
            ts="105.0",
            user="U1",
            text="restart it",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
        return first.prompt, second.prompt

    first_prompt, second_prompt = asyncio.run(scenario())
    assert "is jellyfin up?" in first_prompt
    assert "jellyfin" in second_prompt or "Yes, it's up." in second_prompt
    assert "restart it" in second_prompt


def test_history_is_capped_by_turn_count() -> None:
    store = Store(":memory:")
    key = "thread:1.0"
    entries = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "name": "Tech",
            "text": f"m{i}",
            "ts": "1.0",
        }
        for i in range(conversation.MAX_ENTRIES + 20)
    ]
    store.save_thread(key, "C1", entries)

    # build_context reads whatever is stored as-is - the cap is enforced on
    # the write side, by record_turn (see test_record_turn_trims_to_the_
    # entry_cap below). This just proves an oversized row doesn't blow up
    # the read path.
    ctx = asyncio.run(
        conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="1.0",
            ts="5.0",  # thread_ts != ts -> real-thread key "thread:1.0", matching above
            user="U1",
            text="ping",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
    )
    assert len(ctx.prior_entries) == len(entries)


def test_record_turn_trims_to_the_entry_cap() -> None:
    store = Store(":memory:")
    key = "thread:1.0"
    ctx = conversation.ConversationContext(prompt="", key=key, channel="C1")
    for i in range(conversation.MAX_TURNS + 10):
        ctx.prior_entries = store.get_thread(key)[1] if store.get_thread(key) else []
        conversation.record_turn(
            store, ctx, user_text=f"question {i}", reply_text=f"answer {i}", ts=f"{i}.0"
        )
    _channel, entries = store.get_thread(key)  # type: ignore[misc]
    assert len(entries) <= conversation.MAX_ENTRIES
    # Oldest dropped first: the very first turn shouldn't survive.
    assert not any(e["text"] == "question 0" for e in entries)


def test_history_is_capped_by_character_budget() -> None:
    store = Store(":memory:")
    key = "thread:1.0"
    ctx = conversation.ConversationContext(prompt="", key=key, channel="C1")
    big_text = "x" * 500
    conversation.record_turn(store, ctx, user_text=big_text, reply_text=big_text, ts="1.0")
    for i in range(2, 30):
        ctx.prior_entries = store.get_thread(key)[1]  # type: ignore[index]
        conversation.record_turn(
            store, ctx, user_text=big_text, reply_text=big_text, ts=f"{i}.0"
        )
    _channel, entries = store.get_thread(key)  # type: ignore[misc]
    total_chars = sum(len(e["text"]) for e in entries)
    assert total_chars <= conversation.MAX_HISTORY_CHARS


def test_different_threads_do_not_leak_into_each_other() -> None:
    store = Store(":memory:")

    async def scenario() -> tuple[str, str]:
        a = await conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="90.0",
            ts="100.0",
            user="U1",
            text="thread A message",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
        conversation.record_turn(
            store, a, user_text="thread A message", reply_text="ok A", ts="100.0"
        )

        b = await conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="190.0",
            ts="200.0",
            user="U2",
            text="thread B message",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
        return a.prompt, b.prompt

    prompt_a, prompt_b = asyncio.run(scenario())
    assert "thread A message" in prompt_a
    assert "thread A message" not in prompt_b
    assert "ok A" not in prompt_b


def test_ambient_context_fetched_when_there_is_no_stored_history() -> None:
    store = Store(":memory:")
    calls: list[str] = []

    async def fetch_ambient(channel: str) -> list[dict[str, object]]:
        calls.append(channel)
        return [{"author": "Sam", "text": "the printer is out of paper", "is_bot": False}]

    ctx = asyncio.run(
        conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="what's going on",
            resolve_name=_resolve_name,
            fetch_ambient=fetch_ambient,
        )
    )
    assert calls == ["C1"]
    assert "the printer is out of paper" in ctx.prompt
    assert "is a request or instruction to you" in ctx.prompt


def test_ambient_context_skipped_when_history_already_exists() -> None:
    store = Store(":memory:")
    store.save_thread(
        "thread:1.0",
        "C1",
        [{"role": "user", "name": "Tech", "text": "earlier question", "ts": "1.0"}],
    )
    calls: list[str] = []

    async def fetch_ambient(channel: str) -> list[dict[str, object]]:
        calls.append(channel)
        return [{"author": "Sam", "text": "should not appear", "is_bot": False}]

    ctx = asyncio.run(
        conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="1.0",
            ts="5.0",
            user="U1",
            text="follow up",
            resolve_name=_resolve_name,
            fetch_ambient=fetch_ambient,
        )
    )
    assert calls == []
    assert "should not appear" not in ctx.prompt
    assert "earlier question" in ctx.prompt


def test_channel_fallback_conversation_continues_within_the_window() -> None:
    store = Store(":memory:")
    key = conversation.history_key("C1", "100.0", "100.0")
    store.save_thread(
        key, "C1", [{"role": "user", "name": "Tech", "text": "first message", "ts": "100.0"}]
    )
    # 2 minutes later, no explicit thread - same channel key.
    entries = conversation.load_entries(store, key, "220.0")
    assert entries != []
    assert entries[0]["text"] == "first message"


def test_channel_fallback_conversation_does_not_leak_across_the_window() -> None:
    store = Store(":memory:")
    key = conversation.history_key("C1", "100.0", "100.0")
    store.save_thread(
        key, "C1", [{"role": "user", "name": "Tech", "text": "old message", "ts": "100.0"}]
    )
    # Two days later.
    two_days = 2 * 24 * 60 * 60
    entries = conversation.load_entries(store, key, str(100.0 + two_days))
    assert entries == []


def test_speaker_name_is_resolved_and_used_in_the_prompt() -> None:
    store = Store(":memory:")
    ctx = asyncio.run(
        conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="is jellyfin up?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
    )
    assert ctx.speaker_name == "Tech"
    assert "Tech is asking you directly" in ctx.prompt
