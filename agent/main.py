"""Wiring only: build every piece and run the long-lived process.

Construction order matters here. `slack_app.build()` needs a Runner right away to
wire its Slack event handlers, but those handlers only call `.run()` at message
time, long after startup. `_AgentHandle` stands in for the real `Agent` so `app`
can be built first, then `notify` (which needs `app.client`), then the real
`Agent`, then the `Ticker`. A later task adds an `audit` callback to `Agent` that
closes over `notify`; building `Agent` after `app` here means that task only adds
an argument instead of reordering this file.
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
# nothing in this file calls them directly. Tasks 8-11 add media, photos,
# household, and memory to this line; only comms and infra exist so far.
from agent.tools import comms, infra  # noqa: F401

TICK_SECONDS = 60


class _AgentHandle:
    """Placeholder Runner bound to the real Agent once it exists.

    Lets `app` be constructed before `Agent` without `Agent` needing to exist
    up front: Slack handlers close over this handle, not over the eventual
    Agent, and only call `.run()` well after `bind()` has been called.
    """

    def __init__(self) -> None:
        self._target: Runner | None = None

    def bind(self, target: Runner) -> None:
        self._target = target

    async def run(self, prompt: str, *, priority: str, system: str) -> str:
        if self._target is None:
            raise RuntimeError("agent handle used before the real Agent was bound")
        return await self._target.run(prompt, priority=priority, system=system)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = config.load()
    store = Store(settings.db_path)

    handle = _AgentHandle()
    app = slack_app.build(handle, store, settings.slack_bot_token)

    async def notify(channel: str, text: str) -> None:
        await app.client.chat_postMessage(channel=channel, text=text)

    agent = Agent(settings, store)
    # Agent.run narrows `priority` to Literal["family", "daemon"] while Runner
    # (here and in slack_app/tick) declares it as plain `str`, so every caller
    # in this codebase only ever passes one of those two literals. The cast is
    # safe; a real bad value would still raise at the API boundary in model.py.
    handle.bind(cast(Runner, agent))

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
