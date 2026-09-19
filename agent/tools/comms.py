import os
from typing import Any

import httpx

from agent.tools.base import tool

TIMEOUT = httpx.Timeout(15.0)


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
    r = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
        json={"channel": channel, "text": text},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]
