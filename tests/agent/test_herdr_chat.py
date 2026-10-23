import asyncio
from pathlib import Path
from typing import Any

from agent import herdr_chat, slack_app


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, priority: str, system: str) -> str:
        self.calls.append((prompt, priority))
        return f"answered: {prompt}"


class FakeReactionsClient:
    async def reactions_add(self, **kwargs: Any) -> Any:
        return None

    async def reactions_remove(self, **kwargs: Any) -> Any:
        return None


def test_build_prompt_names_the_reply_file(tmp_path: Path) -> None:
    path = tmp_path / "reply.txt"
    prompt = herdr_chat.build_prompt("what is c-homelab doing?", path)
    assert "what is c-homelab doing?" in prompt
    assert str(path) in prompt
    assert "herdr agent prompt" in prompt


def test_blocked_agent_becomes_a_plain_reply(tmp_path: Path) -> None:
    text = herdr_chat.reply_from(
        "", '{"error":{"code":"agent_blocked"}}', 1, tmp_path / "missing.txt"
    )
    assert "waiting on a question" in text


def test_reply_file_wins_over_a_failed_exit(tmp_path: Path) -> None:
    path = tmp_path / "reply.txt"
    path.write_text("c-homelab is idle\n")
    assert herdr_chat.reply_from("", "nope", 1, path) == "c-homelab is idle"


def test_slack_uses_herdr_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_HERDR_AGENT", "homelab")
    monkeypatch.setattr(herdr_chat, "ask", lambda text: f"herdr says {text}")
    agent = FakeAgent()
    said: list[dict[str, str]] = []

    async def say(**kwargs: str) -> None:
        said.append(kwargs)

    asyncio.run(
        slack_app.handle_message(
            agent=agent,
            text="<@U1> what is running?",
            thread_ts="1.1",
            say=say,
            client=FakeReactionsClient(),
            channel="C1",
            ts="1.1",
        )
    )
    assert agent.calls == []
    assert said[0]["thread_ts"] == "1.1"
    assert "what is running?" in said[0]["text"]
    assert "herdr says" in said[0]["text"]
