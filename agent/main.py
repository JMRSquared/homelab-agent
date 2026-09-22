"""Wiring only: build every piece and run the long-lived process.

Construction order: `Agent` first, then `app` (Slack needs a working `Runner`
immediately to wire its event handlers), then `notify` (needs `app.client`),
then `agent.set_audit(notify)`, then the `Ticker`. `Agent` takes no audit
parameter at construction time - `app`, and therefore `notify`, don't exist
yet when `Agent` is built - so the audit callback that mirrors every tool
call to the audit channel is attached afterwards with `Agent.set_audit`, a setter,
rather than threaded through `Agent.__init__`.
"""

import asyncio
import logging
from typing import cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from agent import config, slack_app, tick
from agent.model import Agent
from agent.slack_app import ConversationsClient, Runner
from agent.store import Store

# Tool registration happens by import side effect (see agent/tools/base.py's
# `@tool` decorator), so every tool module must be imported here even though
# nothing in this file calls them directly.
from agent.tools import (  # noqa: F401
    backup,
    comms,
    household,
    incidents,
    infra,
    jobs,
    mail,
    media,
    memory,
    mt5_screenshot,
    photos,
    selftest_tool,
    usage,
    vision,
)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load()
    store = Store(settings.db_path)

    agent = Agent(settings, store)
    # Agent.run narrows `priority` to Literal["family", "daemon"] while Runner
    # (here and in slack_app/tick) declares it as plain `str`, so every caller
    # in this codebase only ever passes one of those two literals. The cast is
    # safe; a real bad value would still raise at the API boundary in model.py.
    app = slack_app.build(cast(Runner, agent), store, settings.slack_bot_token)

    # Before announcing anything: confirm the configured channels actually
    # resolve and the bot is a member. The live workspace turned out to have
    # none of the channel names this code had hardcoded, and a bare
    # `channel_not_found` from chat_postMessage gives no clue which env var
    # is wrong. preflight_channels() never raises (logs and reports
    # `missing` instead), but it's wrapped the same defensive way as the
    # notify guard below regardless, so a future regression in it can't
    # crash-loop startup either.
    try:
        # AsyncWebClient's users_conversations() returns a response object
        # that behaves like a Mapping (subscriptable, .get()) but doesn't
        # structurally satisfy ConversationsClient's Mapping[str, Any]
        # return type under mypy --strict - the same shape of mismatch the
        # `cast(Runner, agent)` above works around.
        await slack_app.preflight_channels(cast(ConversationsClient, app.client))
    except Exception:
        logging.exception("slack channel preflight raised unexpectedly")

    async def notify(channel: str, text: str) -> None:
        await app.client.chat_postMessage(channel=channel, text=text)

    async def audit(text: str) -> None:
        """Mirror every tool call to the configured audit channel.

        The channel is bound here rather than inside `Agent`, which has no
        business knowing Slack channel names. A hardcoded "#agent-log" in
        the tool loop is what silently broke the audit trail once already.
        """
        await notify(slack_app.CH_LOG, text)

    agent.set_audit(audit)

    ticker = tick.Ticker(cast(tick.Runner, agent), store, notify)
    scheduler = AsyncIOScheduler()
    if settings.tick_seconds > 0:
        scheduler.add_job(ticker.once, "interval", seconds=settings.tick_seconds, max_instances=1)
        scheduler.start()
    else:
        # AGENT_TICK_SECONDS=0: no autonomous sweep at all. Nothing to
        # schedule, so the scheduler is never even started - only Slack is
        # active. Deliberate operational off switch (see config.py), not an
        # error: log it plainly so it's obvious from the journal why the
        # agent isn't acting on its own.
        logging.info(
            "AGENT_TICK_SECONDS=0: autonomous tick disabled, Slack-only mode"
        )

    try:
        await notify(slack_app.CH_HOMELAB, ":satellite: homelab agent online")
    except Exception:
        # chat_postMessage raises on not_in_channel/channel_not_found - most
        # likely on first run, before the bot has been invited to #homelab.
        # Unguarded, this raised before the listener ever started, so
        # Restart=always crash-looped the service every 10s and the agent
        # never answered Slack at all, not even to report why. The listener
        # must start regardless; see the same reasoning already applied to
        # the audit callback in agent/model.py.
        logging.exception("failed to post the startup announcement to #homelab")
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    # slack_bolt's start_async has no return annotation even under py.typed.
    await handler.start_async()  # type: ignore[no-untyped-call]


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
