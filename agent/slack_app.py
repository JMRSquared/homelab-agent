import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from slack_bolt.app.async_app import AsyncApp

from agent import conversation, slack_thinking
from agent.brain import Brain
from agent.model import use_step_hook
from agent.prompts import MT5_GUARDRAILS
from agent.slack_format import to_mrkdwn
from agent.store import Store
from agent.tools import comms

# Read once at import, the same way agent/config.py's Settings are read once
# at process start: the env file is in place before the process starts and
# doesn't change under it, so this is "call time" in the sense that matters
# for a long-running daemon. Kept as plain module constants (not functions)
# because agent/main.py reads `slack_app.CH_HOMELAB` as an attribute, and
# changing that call site is out of scope here.
#
# These are destinations the agent posts to on its own initiative (status
# updates, the tool-call audit trail). They are not a gate on where it
# listens or replies - it replies in every channel it's a member of, plus
# DMs, with no allowlist and no separate "family" channel.
CH_HOMELAB = os.environ.get("SLACK_CHANNEL_STATUS", "#homelab-alerts")
CH_LOG = os.environ.get("SLACK_CHANNEL_LOG", "#homelab-agent-log")
"""Channel for the per-tool-call audit trail. Set SLACK_CHANNEL_LOG empty to
turn it off: the SQLite event store still records every call, so the record
survives, it just stops being announced."""


def audit_channel_enabled() -> bool:
    """False when the owner has turned the audit channel off.

    Checked at call time rather than import, so flipping the env var and
    restarting is enough. Posting to an empty channel id would otherwise
    fail once per tool call and bury the journal in handled exceptions.
    """
    return bool(CH_LOG.strip())

# Which env var and role name each channel constant maps to, for the startup
# preflight's log lines and for anyone auditing what's configurable.
_CHANNEL_ROLES: tuple[tuple[str, str, str], ...] = (
    ("SLACK_CHANNEL_STATUS", "status", CH_HOMELAB),
    *((("SLACK_CHANNEL_LOG", "log", CH_LOG),) if CH_LOG.strip() else ()),
)

MENTION = re.compile(r"<@[A-Z0-9]+>\s*")

logger = logging.getLogger(__name__)

# The single human-facing system prompt, used for every reply regardless of
# which channel or DM it came in on. There is no separate technical voice
# and family voice - one plain-language voice for everyone.
SYSTEM_CHAT = (
    "You are the household assistant for Tech's family, running on their home server. "
    "You manage the homelab and answer questions for everyone in the house. "
    "You act on your own judgement without asking permission. "
    "Prefer doing the thing over describing how to do it. "
    "Answer plainly and briefly, the way you'd explain something to a relative over "
    "dinner, not a colleague. Whoever's asking is not assumed to be an engineer: "
    "skip jargon, IDs, and command names unless someone asks for them directly. "
    + MT5_GUARDRAILS
)


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


class ConversationsClient(Protocol):
    async def users_conversations(self, **kwargs: Any) -> Mapping[str, Any]: ...


class ReactionsClient(Protocol):
    async def reactions_add(self, **kwargs: Any) -> Any: ...
    async def reactions_remove(self, **kwargs: Any) -> Any: ...


# Trivial to change - the user will have opinions. A reply can take several
# seconds while the model runs tools with no other feedback that the agent
# heard the message at all, so REACTION_WORKING goes on as soon as
# handle_message starts and comes off again once it's done, replaced by
# whichever of the other two actually happened.
REACTION_WORKING = "hourglass_flowing_sand"
REACTION_SUCCESS = "white_check_mark"
REACTION_FAILURE = "x"

# The exact string agent/model.py's _complete returns when MAX_TOOL_ROUNDS is
# hit. Not an exception - a returned value - so it can't be caught the way
# agent.run() raising can be; it has to be recognised by content. Kept as a
# constant here rather than imported from agent.model, since slack_app.py
# otherwise has no dependency on that module and the two are decoupled by
# the Runner protocol on purpose.
_ROUND_CAP_MESSAGE = "stopped: exceeded the tool-call round limit"


async def _default_resolve_name(user_id: str) -> str:
    """Resolve a Slack user id to a display name off the event loop.

    `comms.resolve_user_name` does a blocking `httpx` call (cached after the
    first lookup, same as every other tool in this codebase) - run through
    `asyncio.to_thread` for the same reason `agent/model.py` runs tool
    dispatch that way: a slow Slack API call must not stall the websocket.
    """
    return await asyncio.to_thread(comms.resolve_user_name, user_id)


async def _default_fetch_ambient(channel: str) -> list[dict[str, Any]]:
    """Fetch recent channel messages as ambient background, off the event
    loop, for the same reason as `_default_resolve_name`.

    Raises on any Slack-side failure (unknown channel, missing scope,
    network error) - `conversation.build_context` is the layer that decides
    a failed ambient fetch means "continue without it", not this function.
    """
    out = await asyncio.to_thread(comms.slack_history, channel, conversation.AMBIENT_LIMIT)
    return list(out["messages"])


async def _default_fetch_channel_info(channel: str) -> conversation.ChannelInfo:
    """Fetch this channel's name/topic/purpose, off the event loop, for the
    same reason as `_default_resolve_name` - `comms.channel_info` does a
    blocking `httpx` call, cached process-lifetime after the first lookup
    per channel (see `comms._channel_info`).

    Raises on any Slack-side failure, same convention as
    `_default_fetch_ambient` - `conversation.build_context` decides what a
    failure here means (fall back to a name-only frame), not this function.
    """
    return await asyncio.to_thread(comms.channel_info, channel)


def _brain() -> Brain:
    # Mirrors agent/tools/memory.py's own `_brain()` exactly - same env var,
    # same default path, same "construct fresh, the file is the state" model
    # (see agent/brain.py's docstring). Duplicated rather than imported from
    # memory.py because that module's job is tool registration, not being a
    # library this one depends on.
    return Brain(os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md"))


async def _default_fetch_channel_notes(topic: str) -> str:
    """Read whatever this agent has previously recorded about a channel
    (see `conversation.channel_brain_topic`), off the event loop - `Brain`
    does blocking file I/O, same reasoning as every other default fetcher
    here running through `asyncio.to_thread`.
    """
    return await asyncio.to_thread(_brain().read, topic)


def reply_without_mention() -> bool:
    """Whether a plain channel message (no @mention) gets a reply.

    Read at call time, like every other setting in this codebase, so flipping
    `SLACK_REPLY_WITHOUT_MENTION` in the env file takes effect on the next
    message without a restart-and-recompile of anything but the process.
    Defaults on: talk to the agent like a person in any channel it's in. Set
    it to "0" to fall back to mention-required in channels (DMs never need a
    mention either way).
    """
    return os.environ.get("SLACK_REPLY_WITHOUT_MENTION", "1") != "0"


def should_ignore(event: dict[str, Any]) -> bool:
    """Unconditional guard: never answer the bot's own traffic.

    Slack echoes the bot's own posts (and any other bot's posts) back through the
    `message` event. Without this guard a channel where the bot posts and also
    listens becomes an infinite reply loop.

    Any `subtype` at all is a reason to ignore, not just `"bot_message"`. A real
    human message carries no `subtype`; edits (`message_changed`), deletions
    (`message_deleted`), and other non-post variants carry no top-level `text`,
    so letting one through means answering an empty prompt at family priority.
    """
    if event.get("bot_id") is not None:
        return True
    return event.get("subtype") is not None


def needs_mention(event: dict[str, Any]) -> bool:
    """Whether this event only gets a reply if it carries an @mention.

    DMs never need one: a direct message is unambiguously addressed to the
    bot. Everywhere else (channels, groups) this follows
    `reply_without_mention()`.
    """
    if event.get("channel_type") == "im":
        return False
    return not reply_without_mention()


async def _react(
    client: ReactionsClient, method: str, *, channel: str, ts: str, name: str
) -> None:
    """Add or remove one reaction, best-effort.

    Never raises. Slack routinely returns `already_reacted`, `no_reaction`,
    `message_not_found`, or a plain rate limit for this call, none of which
    should cost the user their actual answer - a decoration that can
    swallow the real work is worse than no decoration. Same rule the audit
    callback in agent/model.py follows for the same reason.
    """
    try:
        await getattr(client, method)(channel=channel, timestamp=ts, name=name)
    except Exception:
        logger.exception("Slack %s(%r) failed on %s/%s", method, name, channel, ts)


async def _run_agent(agent: Runner, prompt: str) -> tuple[str, bool]:
    """Run the model and return (text to post, whether it counts as a
    success for reaction purposes).

    A tool the model called returning an error is still a successful
    *reply* - "the AdGuard key is missing" answers the question that was
    asked. Failure, for the reaction, means the reply itself failed: the
    model call raised, or the tool loop hit MAX_TOOL_ROUNDS and gave up
    without a real answer. Either way the user still gets text explaining
    what happened, never a bare reaction with no message.
    """
    try:
        answer = await agent.run(prompt, priority="family", system=SYSTEM_CHAT)
    except Exception:
        logger.exception("agent.run raised while handling a Slack message")
        return (
            "Sorry, I ran into an error and couldn't finish answering that. "
            "It's been logged - try again in a bit.",
            False,
        )
    return answer, answer != _ROUND_CAP_MESSAGE


async def handle_message(
    *,
    agent: Runner,
    text: str,
    thread_ts: str,
    say: Any,
    client: ReactionsClient,
    channel: str,
    ts: str,
    store: Store | None = None,
    user: str | None = None,
    resolve_name: Callable[[str], Awaitable[str]] | None = None,
    fetch_ambient: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    fetch_channel_info: Callable[[str], Awaitable[conversation.ChannelInfo]] | None = None,
    fetch_channel_notes: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    prompt = MENTION.sub("", text).strip()
    # Conversation memory is entirely opt-in on `store`: every existing
    # caller in the test suite (and `Ticker`, if it ever grows one) that
    # doesn't pass a `Store` gets the exact old behaviour - the bare
    # mention-stripped text as the prompt, nothing prepended. Only
    # `build()`'s real Slack listener passes `store`, so this is where
    # memory turns on for production traffic and nowhere else.
    run_prompt = prompt
    ctx: conversation.ConversationContext | None = None
    if store is not None:
        try:
            ctx = await conversation.build_context(
                store=store,
                channel=channel,
                thread_ts=thread_ts,
                ts=ts,
                user=user,
                text=prompt,
                resolve_name=resolve_name or _default_resolve_name,
                fetch_ambient=fetch_ambient or _default_fetch_ambient,
                fetch_channel_info=fetch_channel_info or _default_fetch_channel_info,
                fetch_channel_notes=fetch_channel_notes or _default_fetch_channel_notes,
            )
            run_prompt = ctx.prompt
        except Exception:
            logger.exception(
                "failed to build conversation context for %s/%s; replying without memory",
                channel,
                ts,
            )
    await _react(client, "reactions_add", channel=channel, ts=ts, name=REACTION_WORKING)
    # Thinking Steps: a live view of the reply forming, on top of the
    # reaction above rather than instead of it - see agent/slack_thinking.py's
    # module docstring for the fallback contract. `build()` itself never
    # raises: it returns `None` for anything from the feature flag being off
    # to a client that can't stream to a documented Slack-side refusal, and
    # every path below treats `None` as "post the answer the old way".
    stream: slack_thinking.ThinkingStream | None = None
    try:
        stream = await slack_thinking.build(
            cast(slack_thinking.StreamClient, client),
            channel=channel,
            thread_ts=thread_ts,
            user=user,
        )
    except Exception:
        # build() is written to never raise (see its docstring), but this
        # follows the same belt-and-braces rule as everything else in this
        # function: a decoration failing must never cost the user their
        # reply, so a bug here degrades to the old single-message behaviour
        # instead of breaking the whole request.
        logger.exception("slack_thinking.build failed for %s/%s; replying without it", channel, ts)
    try:
        if stream is not None:
            # Scoped to this one call via a ContextVar, not stored on
            # `agent` - see agent/model.py's StepHook docstring. Every tool
            # call `agent.run` makes underneath this becomes a task card on
            # `stream` while this `with` block is active.
            with use_step_hook(stream):
                reply_text, ok = await _run_agent(agent, run_prompt)
        else:
            reply_text, ok = await _run_agent(agent, run_prompt)
        # The model writes standard Markdown; Slack renders mrkdwn.
        # Converted here, at the point of posting - agent.run()'s return
        # value (and everything upstream of it: the tick path, the
        # #agent-log audit trail) keeps seeing the model's original text
        # untouched.
        mrkdwn_reply = to_mrkdwn(reply_text)
        posted_via_stream = False
        if stream is not None and stream.active:
            posted_via_stream = await stream.stop(mrkdwn_reply)
        if not posted_via_stream:
            try:
                await say(text=mrkdwn_reply, thread_ts=thread_ts)
            except Exception:
                logger.exception("failed to post the Slack reply for %s/%s", channel, ts)
                ok = False
    finally:
        # Always comes off, including when the model call raised or the
        # reply failed to post - a stuck hourglass on every future message
        # in a broken thread would be worse than the reaction never having
        # existed.
        await _react(client, "reactions_remove", channel=channel, ts=ts, name=REACTION_WORKING)
    if store is not None and ctx is not None:
        try:
            conversation.record_turn(store, ctx, user_text=prompt, reply_text=reply_text, ts=ts)
        except Exception:
            # Same rule as the audit callback and the reactions above: a
            # decoration (here, next time's memory) must never cost the
            # user the reply they already got.
            logger.exception("failed to persist conversation history for %s/%s", channel, ts)
    await _react(
        client,
        "reactions_add",
        channel=channel,
        ts=ts,
        name=REACTION_SUCCESS if ok else REACTION_FAILURE,
    )


def build(agent: Runner, store: Store, bot_token: str) -> AsyncApp:
    app = AsyncApp(token=bot_token)

    # The `message` event is the single entry point for everything: DMs,
    # plain channel messages, and channel messages that happen to carry an
    # @mention. There is deliberately no separate `app_mention` handler:
    # subscribing to both fires two events (`app_mention` and `message`) for
    # the same mention in a channel the bot is a member of, which would mean
    # two model calls and two replies in the same thread. `app_mention`
    # carries no information this event lacks once the bot is a member of
    # every channel it needs to answer in.
    @app.event("message")
    async def _message(event: dict[str, Any], say: Any, client: Any) -> None:
        if should_ignore(event):
            return
        text = event.get("text", "")
        if needs_mention(event) and not MENTION.search(text):
            return
        kind = "slack_dm" if event.get("channel_type") == "im" else "slack_message"
        store.record_event(kind, {"user": event.get("user"), "text": text})
        await handle_message(
            agent=agent,
            text=text,
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
            client=client,
            # The reaction goes on the message they just sent, not on the
            # thread's parent - event["ts"] is this message's own
            # timestamp, distinct from thread_ts above (which is the
            # *parent's* ts for a threaded reply, or this same value for a
            # first message). Applies the same way for a DM: event["channel"]
            # is the DM's own channel id and event["ts"] its message ts,
            # exactly as for a channel message.
            channel=event["channel"],
            ts=event["ts"],
            store=store,
            user=event.get("user"),
        )

    return app


async def preflight_channels(client: ConversationsClient) -> list[str]:
    """Verify every configured channel resolves and the bot is a member.

    Workspace channel names change (this module's own defaults once pointed
    at #homelab and #agent-log, channels that don't exist in the real
    workspace at all), and a `channel_not_found` from `chat.postMessage`
    gives no clue which env var is wrong. This checks the configured status
    and log channels against the bot's own conversation list up front and
    logs one clear line per miss, e.g. "configured status channel
    #homelab-alerts not found or bot not a member" - the same failure looks
    identical from the Slack API whether the channel doesn't exist or the
    bot just hasn't been invited, so the message says both.

    Call this from the startup path before announcing the agent is online.
    Never raises: a failure to even list conversations (missing scope, token
    problem) is logged and every configured channel is reported missing,
    since that's the honest state - we couldn't verify any of them - but
    startup must continue either way. Returns the env var names of whichever
    channels didn't resolve, for a caller that wants to act on it
    programmatically instead of just reading the log.
    """
    try:
        joined: set[str] = set()
        cursor: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "types": "public_channel,private_channel",
                "limit": 200,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.users_conversations(**kwargs)
            joined |= {f"#{c['name']}" for c in resp.get("channels", [])}
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break
    except Exception:
        logger.exception("slack channel preflight failed to list the bot's conversations")
        return [var for var, _role, _channel in _CHANNEL_ROLES]

    missing = []
    for var, role, channel in _CHANNEL_ROLES:
        if channel not in joined:
            missing.append(var)
            logger.warning("configured %s channel %s not found or bot not a member", role, channel)
    return missing
