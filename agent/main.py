"""Wiring only: build every piece and run the long-lived process.

Construction order: `Agent` first, then `app` (Slack needs a working `Runner`
immediately to wire its event handlers), then `notify` (needs `app.client`),
then `agent.set_audit(notify)`, then the `Ticker`. `Agent` takes no audit
parameter at construction time - `app`, and therefore `notify`, don't exist
yet when `Agent` is built - so the audit callback that mirrors every tool
call to `#agent-log` is attached afterwards with `Agent.set_audit`, a setter,
rather than threaded through `Agent.__init__`.
"""

import asyncio
import logging
from typing import cast

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from agent import config, slack_app, tick
from agent.model import Agent
from agent.slack_app import Runner
from agent.store import Store

# Tool registration happens by import side effect (see agent/tools/base.py's
# `@tool` decorator), so every tool module must be imported here even though
# nothing in this file calls them directly.
from agent.tools import comms, household, infra, media, memory, photos  # noqa: F401

TICK_SECONDS = 60


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

    async def notify(channel: str, text: str) -> None:
        await app.client.chat_postMessage(channel=channel, text=text)

    agent.set_audit(notify)

    ticker = tick.Ticker(cast(tick.Runner, agent), store, notify)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(ticker.once, "interval", seconds=TICK_SECONDS, max_instances=1)
    scheduler.start()

    await notify(slack_app.CH_HOMELAB, ":satellite: homelab agent online")
    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    # slack_bolt's start_async has no return annotation even under py.typed.
    await handler.start_async()  # type: ignore[no-untyped-call]


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
