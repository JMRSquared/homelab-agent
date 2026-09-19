import asyncio
import json
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
                        out = dispatch(tool_name, args)
                self._store.record_event(
                    "tool_call", {"tool": tool_name, "args": args, "out": out}
                )
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
