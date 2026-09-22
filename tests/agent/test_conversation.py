import asyncio

from agent import brain, conversation
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


def test_ambient_context_is_fetched_even_mid_thread_with_stored_history() -> None:
    """Ambient channel background is no longer gated on "no stored history" -
    the owner asked for recent messages to inform every reply, not only the
    first one in a conversation. Stored thread history and ambient channel
    background are additive: one is this specific exchange, the other is
    the wider channel it's happening in."""
    store = Store(":memory:")
    store.save_thread(
        "thread:1.0",
        "C1",
        [{"role": "user", "name": "Tech", "text": "earlier question", "ts": "1.0"}],
    )
    calls: list[str] = []

    async def fetch_ambient(channel: str) -> list[dict[str, object]]:
        calls.append(channel)
        return [{"author": "Sam", "text": "should still appear", "is_bot": False}]

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
    assert calls == ["C1"]
    assert "should still appear" in ctx.prompt
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


# --- channel framing --------------------------------------------------


async def _channel_info(name: str, topic: str = "", purpose: str = "") -> conversation.ChannelInfo:
    return {"name": name, "topic": topic, "purpose": purpose}


def test_channel_frame_uses_topic_and_purpose_when_present() -> None:
    async def fetch_info(channel: str) -> conversation.ChannelInfo:
        return {
            "name": "homelab-income",
            "topic": "money stuff",
            "purpose": "Autonomous passive-income earnings. Daily per-app totals.",
        }

    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="C9",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="why are we not making money",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=fetch_info,
        )
    )
    assert "homelab-income" in ctx.prompt
    assert "Autonomous passive-income earnings" in ctx.prompt
    assert "money stuff" in ctx.prompt


def test_undescribed_channel_still_produces_a_usable_frame() -> None:
    """A brand-new channel with no topic or purpose must still work on its
    first message - the frame falls back to the channel's name and says
    explicitly that this is a weaker signal, never silently failing."""

    async def fetch_info(channel: str) -> conversation.ChannelInfo:
        return {"name": "homelab-mail", "topic": "", "purpose": ""}

    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="C9",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="anything new?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=fetch_info,
        )
    )
    assert "homelab-mail" in ctx.prompt
    assert "no configured topic or purpose" in ctx.prompt
    assert "weaker signal" in ctx.prompt


def test_channel_frame_present_by_default_with_no_fetcher_supplied() -> None:
    """`build_context` must never fail or omit framing just because a
    caller (or an older test) doesn't pass fetch_channel_info - the default
    still frames on the channel id/name alone."""
    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="#homelab-net",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="anything new?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
        )
    )
    assert "homelab-net" in ctx.prompt


def test_channel_frame_present_on_every_message_not_only_cold_start() -> None:
    """Requirement 2: framing must appear mid-thread too, not just on the
    first message of a conversation."""
    store = Store(":memory:")
    store.save_thread(
        "thread:1.0", "C1", [{"role": "user", "name": "Tech", "text": "earlier", "ts": "1.0"}]
    )
    calls: list[str] = []

    async def fetch_info(channel: str) -> conversation.ChannelInfo:
        calls.append(channel)
        return {"name": "homelab-income", "topic": "", "purpose": "money stuff"}

    ctx = asyncio.run(
        conversation.build_context(
            store=store,
            channel="C1",
            thread_ts="1.0",
            ts="5.0",
            user="U1",
            text="follow up",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=fetch_info,
        )
    )
    assert calls == ["C1"]
    assert "money stuff" in ctx.prompt


def test_channel_frame_states_it_resolves_ambiguity_not_a_cage() -> None:
    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="C9",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="is the zfs pool healthy?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=lambda c: _channel_info("homelab-mail"),
        )
    )
    # The actual, explicit question the person asked is still in the prompt
    # untouched - the frame is a default, not a rewrite of their question.
    assert "is the zfs pool healthy?" in ctx.prompt
    assert "does not override" in ctx.prompt or "never overrides" in ctx.prompt


def test_channel_frame_includes_brain_notes_when_present() -> None:
    async def fetch_info(channel: str) -> conversation.ChannelInfo:
        return {"name": "homelab-mt5", "topic": "", "purpose": ""}

    async def fetch_notes(topic: str) -> str:
        assert topic == conversation.channel_brain_topic("homelab-mt5")
        return "people here ask about open positions and EA status"

    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="C9",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="how are we doing",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=fetch_info,
            fetch_channel_notes=fetch_notes,
        )
    )
    assert "open positions and EA status" in ctx.prompt


def test_channel_frame_survives_a_failing_fetcher() -> None:
    async def broken_info(channel: str) -> conversation.ChannelInfo:
        raise RuntimeError("slack down")

    async def broken_notes(topic: str) -> str:
        raise RuntimeError("disk error")

    ctx = asyncio.run(
        conversation.build_context(
            store=Store(":memory:"),
            channel="#homelab-movies",
            thread_ts="1.0",
            ts="1.0",
            user="U1",
            text="anything new?",
            resolve_name=_resolve_name,
            fetch_ambient=_no_ambient,
            fetch_channel_info=broken_info,
            fetch_channel_notes=broken_notes,
        )
    )
    assert "homelab-movies" in ctx.prompt


def test_channel_brain_topic_is_derived_from_the_channel_name() -> None:
    assert conversation.channel_brain_topic("homelab-income") == "channel-homelab-income"
    # Sanitized against a channel name containing characters brain topics
    # reject, never raising and never producing an invalid topic.
    assert brain.TOPIC_PATTERN.match(conversation.channel_brain_topic("weird#name!"))


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
