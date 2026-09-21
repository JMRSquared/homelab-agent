import os
import re
from typing import Any

import httpx

from agent.slack_format import to_mrkdwn
from agent.tools.base import tool

TIMEOUT = httpx.Timeout(15.0)

# Name->id and id->name are both stable for a workspace over a process's
# lifetime, so both caches live at module scope and are populated lazily -
# looked up once, reused for every later call in the same process, per the
# spec's "do not look it up per call" requirement. Module-level rather than
# per-call-site because slack_history and slack_thread_replies both need
# channel resolution, and dispatch() constructs no shared state a tool could
# hang a cache off instead.
_channel_ids: dict[str, str] = {}
_user_names: dict[str, str] = {}

_CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{8,}$")

# Total characters of message text a single slack_history/slack_thread_replies
# call will return, summed across every message returned. A busy channel's
# raw history can run to tens of thousands of characters of text; this caps
# what reaches the model's context regardless of how many messages that
# takes. The result says when this cut a message off rather than silently
# returning fewer messages than were actually asked for.
_TEXT_CAP = 12_000
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


@tool(
    "slack_say",
    "Post a message to a Slack channel. Use #homelab for actions and incidents, "
    "#family for family-facing replies.",
    {
        "type": "object",
        "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
        "required": ["channel", "text"],
        "additionalProperties": False,
    },
)
def slack_say(channel: str, text: str) -> dict[str, Any]:
    # The daemon prompt tells the model to write this text; it writes
    # standard Markdown. Converted to Slack mrkdwn here, at the point of
    # posting - dispatch()'s record of the call, and the #agent-log audit
    # trail mirroring it, still capture the model's original text.
    r = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel, "text": to_mrkdwn(text)},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"}


def _slack_get(method: str, params: dict[str, Any]) -> dict[str, Any]:
    r = httpx.get(
        f"https://slack.com/api/{method}",
        headers=_headers(),
        params={k: v for k, v in params.items() if v is not None},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data: dict[str, Any] = r.json()
    if not data.get("ok"):
        # Slack answers 200 with {"ok": false, "error": "..."} for every
        # application-level failure - an unknown channel, a channel the bot
        # isn't a member of, a missing scope. There is no HTTP status to
        # raise_for_status() on for any of these, so this is the only place
        # that distinguishes "the request failed" from "the request
        # succeeded and returned no messages".
        raise RuntimeError(f"slack {method} failed: {data.get('error', 'unknown_error')}")
    return data


def _load_channel_ids() -> None:
    """Populate the name->id cache from every channel (public and private)
    the bot can see, across as many pages as conversations.list has. Called
    at most once per process, lazily, the first time a name needs
    resolving - see `_channel_ids`."""
    cursor: str | None = None
    while True:
        data = _slack_get(
            "conversations.list",
            {"types": "public_channel,private_channel", "limit": 200, "cursor": cursor},
        )
        for ch in data.get("channels", []):
            name, cid = ch.get("name"), ch.get("id")
            if name and cid:
                _channel_ids[name] = cid
        cursor = data.get("response_metadata", {}).get("next_cursor") or None
        if not cursor:
            return


def resolve_channel(channel: str) -> str:
    """Resolve a channel name ('#homelab-mt5' or 'homelab-mt5') to its
    Slack id via the cached name->id map, built once per process (see
    `_load_channel_ids`) rather than looked up on every call. A raw channel
    id (the model sometimes has one from an earlier tool result) is
    returned unchanged, with no lookup at all.
    """
    if _CHANNEL_ID_RE.match(channel):
        return channel
    name = channel.lstrip("#")
    if not _channel_ids:
        _load_channel_ids()
    if name not in _channel_ids:
        raise ValueError(
            f"no channel named {channel!r} found among the public and private "
            "channels this bot can see - check the name, or the bot may need to "
            "be invited to it"
        )
    return _channel_ids[name]


def _user_name(user_id: str) -> str:
    """Resolve a Slack user id to a human display name via users.info,
    cached for the process lifetime - a raw id like 'U0C31JN9C6N' means
    nothing to the model, but looking every author up on every call would
    be one extra Slack round trip per message in a busy channel."""
    if user_id in _user_names:
        return _user_names[user_id]
    data = _slack_get("users.info", {"user": user_id})
    profile = data.get("user", {})
    name = str(profile.get("real_name") or profile.get("name") or user_id)
    _user_names[user_id] = name
    return name


def _author(msg: dict[str, Any]) -> str:
    user_id = msg.get("user")
    if user_id:
        return _user_name(str(user_id))
    # A bot post (Slack's own Slackbot, an app, or this agent's own
    # slack_say) carries no "user" field - users.info can't resolve a bot_id
    # anyway, so fall back to the bot's own posted username, or its id as a
    # last resort, rather than spending a lookup that would only fail.
    return str(msg.get("username") or msg.get("bot_id") or "unknown")


def _format_messages(raw: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Shape Slack's raw message list into what the model needs, and cap
    the total text returned - see `_TEXT_CAP`. Stops adding messages (rather
    than truncating one mid-message) once the running total would exceed
    the cap, and always returns at least the first message even if that one
    message alone is over the cap, so a single huge message doesn't produce
    an empty, silently-unhelpful result."""
    out: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for msg in raw:
        text = str(msg.get("text") or "")
        if out and total + len(text) > _TEXT_CAP:
            truncated = True
            break
        total += len(text)
        out.append(
            {
                "author": _author(msg),
                "text": text,
                "ts": msg.get("ts"),
                "is_bot": bool(msg.get("bot_id")),
                "thread_ts": msg.get("thread_ts"),
            }
        )
    return out, truncated


@tool(
    "slack_history",
    "Read recent top-level messages from a Slack channel - not thread replies, see "
    "slack_thread_replies for those. This is the only way to see anything said in "
    "Slack that you weren't directly addressed in: a message pointing back at an "
    "earlier report, a conversation you joined late, 'what did X say about Y' - you "
    "cannot answer those from memory alone. `channel` accepts a name ('#homelab-mt5' "
    "or 'homelab-mt5') or a raw Slack channel id. `limit` caps how many messages "
    "come back (default 20, max 100). `oldest`/`latest` are optional Slack "
    "timestamps ('1700000000.000000') bounding the time window - use them to look "
    "further back or narrow in on a period. The result's `truncated` flag means the "
    "text cap cut it short before `limit` was reached; ask again with a narrower "
    "window rather than assuming that's everything.",
    {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
            "oldest": {"type": "string"},
            "latest": {"type": "string"},
        },
        "required": ["channel"],
        "additionalProperties": False,
    },
)
def slack_history(
    channel: str,
    limit: int = _DEFAULT_LIMIT,
    oldest: str | None = None,
    latest: str | None = None,
) -> dict[str, Any]:
    channel_id = resolve_channel(channel)
    data = _slack_get(
        "conversations.history",
        {"channel": channel_id, "limit": limit, "oldest": oldest, "latest": latest},
    )
    messages, truncated = _format_messages(list(data.get("messages", [])))
    return {
        "channel": channel,
        "channel_id": channel_id,
        "messages": messages,
        "truncated": truncated,
        "has_more": bool(data.get("has_more")),
    }


@tool(
    "slack_thread_replies",
    "Read every reply in one Slack thread. Kept separate from slack_history - which "
    "returns top-level messages only - rather than folding thread replies in "
    "automatically: a channel with several long threads would blow the text cap on "
    "every single history call if each one's full thread came back inline, so "
    "fetch a thread's replies only once you actually need them. `channel` is the "
    "same as slack_history. `thread_ts` is the parent message's timestamp - any "
    "message slack_history returns with a non-null `thread_ts` started or belongs "
    "to a thread you can fetch this way.",
    {
        "type": "object",
        "properties": {
            "channel": {"type": "string", "minLength": 1},
            "thread_ts": {"type": "string", "minLength": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT},
        },
        "required": ["channel", "thread_ts"],
        "additionalProperties": False,
    },
)
def slack_thread_replies(
    channel: str, thread_ts: str, limit: int = _DEFAULT_LIMIT
) -> dict[str, Any]:
    channel_id = resolve_channel(channel)
    data = _slack_get(
        "conversations.replies", {"channel": channel_id, "ts": thread_ts, "limit": limit}
    )
    messages, truncated = _format_messages(list(data.get("messages", [])))
    return {
        "channel": channel,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "messages": messages,
        "truncated": truncated,
        "has_more": bool(data.get("has_more")),
    }
