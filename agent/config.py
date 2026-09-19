import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    minimax_api_key: str
    minimax_base_url: str
    model: str
    hostctl_url: str
    hostctl_token: str
    slack_bot_token: str
    slack_app_token: str
    db_path: str
    brain_path: str
    # Seconds between autonomous tick sweeps. 0 disables the tick entirely -
    # main.py skips scheduling it and the process only answers Slack, no
    # autonomous action. An operational knob, not a code change: lets the
    # agent be started for conversational use immediately, with the
    # autonomous loop turned on later once it's trusted, and lets it be
    # turned off again during an incident without stopping the service.
    tick_seconds: int


def _req(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var {name}")
    return value


def load() -> Settings:
    return Settings(
        minimax_api_key=_req("MINIMAX_API_KEY"),
        minimax_base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
        model=os.environ.get("MINIMAX_MODEL", "MiniMax-M3"),
        hostctl_url=os.environ.get("HOSTCTL_URL", "http://10.0.0.2:8710"),
        hostctl_token=_req("HOSTCTL_TOKEN"),
        slack_bot_token=_req("SLACK_BOT_TOKEN"),
        slack_app_token=_req("SLACK_APP_TOKEN"),
        db_path=os.environ.get("AGENT_DB", "/tank/dev/agent/agent.db"),
        brain_path=os.environ.get("AGENT_BRAIN", "/tank/dev/agent/brain.md"),
        tick_seconds=int(os.environ.get("AGENT_TICK_SECONDS", "60")),
    )
