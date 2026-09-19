import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageFunctionToolCall

from agent.config import Settings
from agent.store import Store
from agent.tools.base import dispatch, openai_schema

MAX_CONCURRENCY = 4
FAMILY_RESERVED = 2
MAX_TOOL_ROUNDS = 12


class Agent:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self._client = AsyncOpenAI(
            api_key=settings.minimax_api_key, base_url=settings.minimax_base_url
        )
        self._all = asyncio.Semaphore(MAX_CONCURRENCY)
        self._daemon = asyncio.Semaphore(MAX_CONCURRENCY - FAMILY_RESERVED)
        self._audit: Callable[[str, str], Awaitable[None]] | None = None

    def set_audit(self, audit: Callable[[str, str], Awaitable[None]]) -> None:
        """Attach a callback that mirrors every tool call to a Slack channel.

        Called after construction (see `agent/main.py`'s docstring) since the
        callback itself needs `app.client`, which needs `Agent` to already
        exist. Best-effort: a failure here must never break the tool loop.
        """
        self._audit = audit

    async def _complete(self, messages: list[dict[str, Any]]) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._client.chat.completions.create(
                model=self._s.model,
                messages=cast(Any, messages),
                tools=cast(Any, openai_schema()),
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append(msg.model_dump(exclude_none=True))
            for raw_call in msg.tool_calls:
                args: dict[str, Any] = {}
                out: dict[str, Any]
                tool_name: str | None
                if not isinstance(raw_call, ChatCompletionMessageFunctionToolCall):
                    tool_name = None
                    out = {
                        "ok": False,
                        "error": f"unsupported tool-call type: {type(raw_call).__name__}",
                    }
                else:
                    tool_name = raw_call.function.name
                    try:
                        args = json.loads(raw_call.function.arguments or "{}")
                    except json.JSONDecodeError as exc:
                        out = {
                            "ok": False,
                            "error": f"arguments were not valid JSON: {exc}",
                        }
                    else:
                        # Every tool underneath does blocking I/O (sync httpx
                        # with a 30s+ timeout, blocking DAVClient, file I/O).
                        # Run it off the event loop so a slow/hung tool call
                        # can't stall the Slack websocket, the tick, or the
                        # other concurrency slots sharing this loop.
                        out = await asyncio.to_thread(dispatch, tool_name, args)
                self._store.record_event(
                    "tool_call", {"tool": tool_name, "args": args, "out": out}
                )
                if self._audit is not None:
                    try:
                        await self._audit(
                            "#agent-log",
                            f"`{tool_name}` {json.dumps(args)} -> "
                            f"{'ok' if out['ok'] else out['error']}",
                        )
                    except Exception:  # audit is best-effort and must never break the loop
                        pass
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": raw_call.id,
                        "content": json.dumps(out),
                    }
                )
        return "stopped: exceeded the tool-call round limit"

    async def run(
        self, prompt: str, *, priority: Literal["family", "daemon"], system: str
    ) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        if priority == "daemon":
            async with self._daemon, self._all:
                return await self._complete(messages)
        async with self._all:
            return await self._complete(messages)
