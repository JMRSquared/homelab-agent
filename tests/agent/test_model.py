import asyncio

from agent.model import Agent


def test_family_priority_preempts_daemon(monkeypatch, tmp_path):
    from agent.config import Settings
    from agent.store import Store

    settings = Settings(
        minimax_api_key="k", minimax_base_url="http://x/v1", model="MiniMax-M3",
        hostctl_url="http://h", hostctl_token="t", slack_bot_token="b",
        slack_app_token="a", db_path=str(tmp_path / "d.db"), brain_path=str(tmp_path / "b.md"),
    )
    agent = Agent(settings, Store(settings.db_path))
    order: list[str] = []

    async def fake_call(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "done"

    monkeypatch.setattr(agent, "_complete", fake_call)

    async def scenario():
        daemons = [
            asyncio.create_task(agent.run("d", priority="daemon", system="s"))
            for _ in range(4)
        ]
        await asyncio.sleep(0.01)
        fam = asyncio.create_task(agent.run("f", priority="family", system="s"))
        await fam
        order.append("family")
        await asyncio.gather(*daemons)
        order.append("daemons")

    asyncio.run(scenario())
    assert order == ["family", "daemons"]
