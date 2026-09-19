"""Regression test for I4: the startup announcement must not be able to
crash-loop the service. `chat_postMessage` raises on `not_in_channel` /
`channel_not_found` - realistically likely on first run, before the bot has
been invited to every channel - and it used to run unguarded, before
`handler.start_async()`, so that single failure kept the listener from ever
starting at all under `Restart=always`.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent import config, main
from agent.config import Settings


def _settings(tmp_path: Any) -> Settings:
    return Settings(
        minimax_api_key="k",
        minimax_base_url="http://x/v1",
        model="MiniMax-M3",
        hostctl_url="http://h",
        hostctl_token="t",
        slack_bot_token="xoxb-fake",
        slack_app_token="xapp-fake",
        db_path=str(tmp_path / "d.db"),
        brain_path=str(tmp_path / "b.md"),
    )


def test_startup_announcement_failure_does_not_block_the_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path))

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            raise RuntimeError("not_in_channel")

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    handler_started = AsyncMock()

    class FakeHandler:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start_async(self) -> None:
            await handler_started()

    monkeypatch.setattr(main, "AsyncSocketModeHandler", FakeHandler)

    class FakeScheduler:
        def add_job(self, *a: Any, **kw: Any) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(main, "AsyncIOScheduler", lambda: FakeScheduler())

    asyncio.run(main.amain())

    handler_started.assert_awaited_once()


def test_startup_announcement_success_still_starts_the_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path))

    posted: list[dict[str, Any]] = []

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    handler_started = AsyncMock()

    class FakeHandler:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start_async(self) -> None:
            await handler_started()

    monkeypatch.setattr(main, "AsyncSocketModeHandler", FakeHandler)

    class FakeScheduler:
        def add_job(self, *a: Any, **kw: Any) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(main, "AsyncIOScheduler", lambda: FakeScheduler())

    asyncio.run(main.amain())

    handler_started.assert_awaited_once()
    assert posted and "online" in posted[0]["text"]


if __name__ == "__main__":
    pytest.main([__file__])
