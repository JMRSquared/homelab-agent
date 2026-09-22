import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageFunctionToolCall

from agent.config import Settings
from agent.store import Store
from agent.tools.base import dispatch, openai_schema

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 4
FAMILY_RESERVED = 2
# How many tool-call rounds one request may take before the loop gives up.
# 12 was set when this was a chat assistant. Real infrastructure work needs
# far more: asked to change a cron-driven report, the agent spent twelve
# rounds exploring the host, backing up the script, rewriting it and testing
# the change three ways - correct work that hit the cap before it could
# report back. The cap exists to stop a runaway loop, not to budget effort.
MAX_TOOL_ROUNDS = int(os.environ.get("AGENT_MAX_TOOL_ROUNDS", "40"))



_REASONING_BLOCK = re.compile(
    r"<\s*(think|thinking|reasoning)\s*>.*?<\s*/\s*\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_UNCLOSED_REASONING = re.compile(
    r"<\s*(think|thinking|reasoning)\s*>.*\Z", re.DOTALL | re.IGNORECASE
)


def _strip_reasoning(content: str | None) -> str:
    """Remove a model's chain-of-thought before the text reaches a human.

    MiniMax-M3 emits its reasoning inline in `content`, wrapped in <think>
    tags. That is useful to us and meaningless to the family reading Slack,
    so it never leaves this module. An unclosed opening tag is treated as
    running to the end of the string: a truncated response should lose its
    reasoning rather than publish half of it.
    """
    if not content:
        return ""
    text = _REASONING_BLOCK.sub("", content)
    text = _UNCLOSED_REASONING.sub("", text)
    return text.strip()

class Agent:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._s = settings
        self._store = store
        self._client = AsyncOpenAI(
            api_key=settings.minimax_api_key, base_url=settings.minimax_base_url
        )
        self._all = asyncio.Semaphore(MAX_CONCURRENCY)
        self._daemon = asyncio.Semaphore(MAX_CONCURRENCY - FAMILY_RESERVED)
        self._audit: Callable[[str], Awaitable[None]] | None = None

    def set_audit(self, audit: Callable[[str], Awaitable[None]]) -> None:
        """Attach a callback that mirrors every tool call to a Slack channel.

        Called after construction (see `agent/main.py`'s docstring) since the
        callback itself needs `app.client`, which needs `Agent` to already
        exist. Best-effort: a failure here must never break the tool loop.
        """
        self._audit = audit

    def _record_usage(self, resp: Any, context: str) -> None:
        """Persist the token usage the provider returned with this response,
        if it returned any. Not every provider/route includes `usage` on
        every response, and a mid-tier or misbehaving one could omit it
        entirely - that must never crash the tool loop, so this only logs.
        """
        usage = getattr(resp, "usage", None)
        if usage is None:
            return
        try:
            prompt_tokens = int(usage.prompt_tokens or 0)
            completion_tokens = int(usage.completion_tokens or 0)
            total_tokens = int(usage.total_tokens or (prompt_tokens + completion_tokens))
            self._store.record_usage(
                context=context,
                model=self._s.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception:
            logger.exception("failed to record model usage for context %r", context)

    async def _complete(self, messages: list[dict[str, Any]], context: str) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._client.chat.completions.create(
                model=self._s.model,
                messages=cast(Any, messages),
                tools=cast(Any, openai_schema()),
            )
            self._record_usage(resp, context)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return _strip_reasoning(msg.content)
            messages.append(msg.model_dump(exclude_none=True))
            for raw_call in msg.tool_calls:
                args: dict[str, Any] = {}
                out: dict[str, Any]
                tool_name: str | None
                # What record_event logs for "args" on a JSON-decode failure:
                # the raw string the model actually sent, not the empty dict
                # `args` falls back to. With a mid-tier model occasionally
                # emitting malformed tool arguments, the raw string is the
                # thing an operator most needs to see, and `{}` threw it away.
                logged_args: Any = args
                if not isinstance(raw_call, ChatCompletionMessageFunctionToolCall):
                    tool_name = None
                    out = {
                        "ok": False,
                        "error": f"unsupported tool-call type: {type(raw_call).__name__}",
                    }
                else:
                    tool_name = raw_call.function.name
                    raw_arguments = raw_call.function.arguments or "{}"
                    try:
                        args = json.loads(raw_arguments)
                    except json.JSONDecodeError as exc:
                        out = {
                            "ok": False,
                            "error": f"arguments were not valid JSON: {exc}",
                        }
                        logged_args = raw_arguments
                    else:
                        logged_args = args
                        # Every tool underneath does blocking I/O (sync httpx
                        # with a 30s+ timeout, blocking DAVClient, file I/O).
                        # Run it off the event loop so a slow/hung tool call
                        # can't stall the Slack websocket, the tick, or the
                        # other concurrency slots sharing this loop.
                        out = await asyncio.to_thread(dispatch, tool_name, args)
                self._store.record_event(
                    "tool_call", {"tool": tool_name, "args": logged_args, "out": out}
                )
                if self._audit is not None:
                    try:
                        await self._audit(
                            f"`{tool_name}` {json.dumps(logged_args)} -> "
                            f"{'ok' if out['ok'] else out['error']}"
                        )
                    except Exception:  # audit is best-effort and must never break the loop
                        logger.exception("audit callback failed for tool %s", tool_name)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": raw_call.id,
                        "content": json.dumps(out),
                    }
                )
        return "stopped: exceeded the tool-call round limit"

    async def run(
        self,
        prompt: str,
        *,
        priority: Literal["family", "daemon"],
        system: str,
        context: str | None = None,
    ) -> str:
        """Run one prompt to completion. `context` labels every usage row
        this call produces (see `agent/usage.py`) - which loop or
        conversation drove the cost. Callers that don't pass one (existing
        callers this change doesn't touch, e.g. the 60s tick) fall back to
        `priority` itself, which is still a meaningful bucket ("daemon" vs
        "family") even without a finer label.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        usage_context = context or priority
        if priority == "daemon":
            async with self._daemon, self._all:
                return await self._complete(messages, usage_context)
        async with self._all:
            return await self._complete(messages, usage_context)
