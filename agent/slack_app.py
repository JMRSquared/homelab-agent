import re
from typing import Any, Protocol

from slack_bolt.app.async_app import AsyncApp

from agent.store import Store

CH_HOMELAB = "#homelab"
CH_FAMILY = "#family"
CH_LOG = "#agent-log"

MENTION = re.compile(r"<@[A-Z0-9]+>\s*")

SYSTEM_FAMILY = (
    "You are the household assistant for Tech's family, running on their home server. "
    "You manage the homelab and answer questions for everyone in the house. "
    "You act on your own judgement without asking permission. "
    "Prefer doing the thing over describing how to do it. "
    "Answer plainly and briefly, the way you'd explain something to a relative over "
    "dinner, not a colleague. Family members are not engineers: skip jargon, IDs, and "
    "command names unless someone asks for them directly. "
    "You have no access to the trading VM and must never discuss placing trades, "
    "trading signals, or anything to do with the mt5 VM. If asked, say plainly that "
    "it's off limits and change the subject."
)


class Runner(Protocol):
    async def run(self, prompt: str, *, priority: str, system: str) -> str: ...


def should_ignore(event: dict[str, Any]) -> bool:
    """Guard for the plain `message` event: only ever answer a genuine human DM.

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
    if event.get("subtype") is not None:
        return True
    return event.get("channel_type") != "im"


async def handle_message(*, agent: Runner, text: str, thread_ts: str, say: Any) -> None:
    prompt = MENTION.sub("", text).strip()
    answer = await agent.run(prompt, priority="family", system=SYSTEM_FAMILY)
    await say(text=answer, thread_ts=thread_ts)


def build(agent: Runner, store: Store, bot_token: str) -> AsyncApp:
    app = AsyncApp(token=bot_token)

    @app.event("app_mention")
    async def _mention(event: dict[str, Any], say: Any) -> None:
        if event.get("bot_id") is not None:
            return
        store.record_event(
            "slack_mention", {"user": event.get("user"), "text": event.get("text")}
        )
        await handle_message(
            agent=agent,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    @app.event("message")
    async def _dm(event: dict[str, Any], say: Any) -> None:
        if should_ignore(event):
            return
        store.record_event("slack_dm", {"user": event.get("user"), "text": event.get("text")})
        await handle_message(
            agent=agent,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts") or event["ts"],
            say=say,
        )

    return app
