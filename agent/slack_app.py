import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Protocol

from slack_bolt.app.async_app import AsyncApp

from agent.prompts import MT5_GUARDRAILS
from agent.slack_format import to_mrkdwn
from agent.store import Store

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

# Which env var and role name each channel constant maps to, for the startup
# preflight's log lines and for anyone auditing what's configurable.
_CHANNEL_ROLES: tuple[tuple[str, str, str], ...] = (
    ("SLACK_CHANNEL_STATUS", "status", CH_HOMELAB),
    ("SLACK_CHANNEL_LOG", "log", CH_LOG),
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


async def handle_message(*, agent: Runner, text: str, thread_ts: str, say: Any) -> None:
    prompt = MENTION.sub("", text).strip()
    answer = await agent.run(prompt, priority="family", system=SYSTEM_CHAT)
    # The model writes standard Markdown; Slack renders mrkdwn. Converted
    # here, at the point of posting - agent.run()'s return value (and
    # everything upstream of it: the tick path, the #agent-log audit trail)
    # keeps seeing the model's original text untouched.
    await say(text=to_mrkdwn(answer), thread_ts=thread_ts)


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
    async def _message(event: dict[str, Any], say: Any) -> None:
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
