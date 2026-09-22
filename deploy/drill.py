#!/usr/bin/env python3
"""Ask the live agent a question the way Slack does, and show its working.

This is the teaching harness. It drives the real `Agent` with the real system
prompt and the real tool registry, so what you see here is exactly what a
family member would get — minus Slack. It prints every tool call the model
made, in order, so a failure localises to "wrong tool", "right tool wrong
arguments", or "never even tried".

    python deploy/drill.py "what did we last watch?"
"""

from __future__ import annotations

import asyncio
import os
import sys
import time


def _load_env() -> None:
    for line in open("/etc/homelab-agent/env"):
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.rstrip("\n").split("=", 1)
        os.environ.setdefault(key, value)


async def main() -> int:
    _load_env()

    from agent import config, slack_app
    from agent.model import Agent
    from agent.store import Store
    from agent.tools import (  # noqa: F401  imported for tool registration
        comms,
        household,
        infra,
        mail,
        media,
        memory,
        mt5_screenshot,
        photos,
        vision,
    )
    from agent.tools.base import REGISTRY

    prompt = " ".join(sys.argv[1:]).strip()
    if not prompt:
        print("usage: drill.py <what to ask the agent>")
        return 2

    settings = config.load()
    store = Store(settings.db_path)
    agent = Agent(settings, store)

    calls: list[tuple[str, dict[str, object], bool]] = []

    async def audit(text: str) -> None:
        # The audit callback is the only place every tool call is visible
        # without reaching into the model loop. Parse rather than re-plumb.
        name = text.split("`")[1] if "`" in text else "?"
        calls.append((name, {}, "-> ok" in text))

    agent.set_audit(audit)

    print("=" * 72)
    print("ASKED:", prompt)
    print(f"({len(REGISTRY)} tools available)")
    print("=" * 72)

    started = time.monotonic()
    try:
        answer = await agent.run(prompt, priority="family", system=slack_app.SYSTEM_CHAT)
    except Exception as exc:  # noqa: BLE001 - the drill reports failures, it does not raise
        print(f"\nFAILED after {time.monotonic() - started:.1f}s: {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.monotonic() - started

    print("\nTOOL CALLS, in order:")
    if not calls:
        print("   (none — the model answered without touching the homelab)")
    for i, (name, _args, ok) in enumerate(calls, 1):
        print(f"   {i:2}. {name:<22} {'ok' if ok else 'FAILED'}")

    print(f"\nANSWER ({elapsed:.1f}s):")
    print(answer or "(empty)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
