"""Regression tests for agent/main.py's startup wiring.

I4: the startup announcement must not be able to crash-loop the service.
`chat_postMessage` raises on `not_in_channel`/`channel_not_found` -
realistically likely on first run, before the bot has been invited to every
channel - and it used to run unguarded, before `handler.start_async()`, so
that single failure kept the listener from ever starting at all under
`Restart=always`.

Also covers: the Slack channel preflight runs before the startup notify and
never blocks startup even if it misbehaves, and `AGENT_TICK_SECONDS`
actually gates whether the tick gets scheduled.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent import config, main
from agent.config import Settings


def _settings(tmp_path: Any, *, tick_seconds: int = 60) -> Settings:
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
        tick_seconds=tick_seconds,
    )


def _wire_common(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, tick_seconds: int = 60):
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path, tick_seconds=tick_seconds))

    posted: list[dict[str, Any]] = []

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            posted.append(kwargs)

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    preflight_called = AsyncMock()

    async def fake_preflight(client: Any) -> list[str]:
        await preflight_called()
        return []

    monkeypatch.setattr(main.slack_app, "preflight_channels", fake_preflight)

    handler_started = AsyncMock()

    class FakeHandler:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start_async(self) -> None:
            await handler_started()

    monkeypatch.setattr(main, "AsyncSocketModeHandler", FakeHandler)

    scheduled_jobs: list[dict[str, Any]] = []
    scheduler_starts: list[None] = []

    class FakeScheduler:
        def add_job(self, fn: Any, trigger: str, **kw: Any) -> None:
            scheduled_jobs.append({"fn": fn, "trigger": trigger, **kw})

        def start(self) -> None:
            scheduler_starts.append(None)

    monkeypatch.setattr(main, "AsyncIOScheduler", lambda: FakeScheduler())

    return posted, preflight_called, handler_started, scheduled_jobs, scheduler_starts


def test_startup_announcement_failure_does_not_block_the_listener(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path))

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            raise RuntimeError("not_in_channel")

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    async def fake_preflight(client: Any) -> list[str]:
        return []

    monkeypatch.setattr(main.slack_app, "preflight_channels", fake_preflight)

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
    posted, _preflight, handler_started, _jobs, _sched = _wire_common(monkeypatch, tmp_path)

    asyncio.run(main.amain())

    handler_started.assert_awaited_once()
    assert posted and "online" in posted[0]["text"]


def test_channel_preflight_runs_before_the_startup_notify(tmp_path, monkeypatch):
    """preflight_channels() must run, and run before the notify, so an
    operator sees which configured channel is missing in the journal before
    the (possibly failing) startup post is even attempted."""
    order: list[str] = []
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path))

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            order.append("notify")

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    async def fake_preflight(client: Any) -> list[str]:
        order.append("preflight")
        return ["SLACK_CHANNEL_STATUS"]

    monkeypatch.setattr(main.slack_app, "preflight_channels", fake_preflight)

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

    assert order == ["preflight", "notify"]
    handler_started.assert_awaited_once()


def test_channel_preflight_failure_does_not_block_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: _settings(tmp_path))

    class FakeClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            pass

    class FakeApp:
        client = FakeClient()

    fake_app = FakeApp()
    monkeypatch.setattr(main.slack_app, "build", lambda *a, **kw: fake_app)

    async def broken_preflight(client: Any) -> list[str]:
        raise RuntimeError("missing_scope")

    monkeypatch.setattr(main.slack_app, "preflight_channels", broken_preflight)

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


def test_positive_tick_seconds_schedules_the_job(tmp_path, monkeypatch):
    _posted, _preflight, _handler, jobs, sched_starts = _wire_common(
        monkeypatch, tmp_path, tick_seconds=45
    )

    asyncio.run(main.amain())

    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "interval"
    assert jobs[0]["seconds"] == 45
    assert len(sched_starts) == 1


def test_zero_tick_seconds_schedules_nothing_but_startup_still_completes(tmp_path, monkeypatch):
    _posted, _preflight, handler_started, jobs, sched_starts = _wire_common(
        monkeypatch, tmp_path, tick_seconds=0
    )

    asyncio.run(main.amain())

    assert jobs == []
    assert sched_starts == []
    handler_started.assert_awaited_once()


if __name__ == "__main__":
    pytest.main([__file__])
