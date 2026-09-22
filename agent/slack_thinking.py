"""Slack Thinking Steps: stream a reply live and show each tool call as a
task card, instead of leaving the owner staring at an hourglass reaction for
the whole time a reply takes to form.

Layered on top of what already exists rather than replacing any of it:
- The ⏳ -> ✅/❌ reactions in `agent/slack_app.py` keep working exactly as
  before; this is a second, richer channel of feedback, not a replacement.
- The `#homelab-agent-log` audit trail (`Agent._audit`) keeps working
  exactly as before too - this hooks into the same tool loop via a second,
  parallel mechanism (`agent.model.StepHook` / `use_step_hook`), because the
  audit callback is process-global (one text stream to one channel) while a
  Thinking Steps message is scoped to one specific reply in one specific
  thread, and `Agent` is shared across every concurrent Slack message and
  the 60s tick.

Request/response shapes below were confirmed against Slack's own docs
(chat.startStream / chat.appendStream / chat.stopStream, the `plan`/
`task_card` block reference, and the AI agents developer guide) rather than
guessed - see `.superpowers/sdd/2026-09-19-homelab-agent/thinking-steps-report.md`
for the exact citations and anything the docs left ambiguous.

Display mode: **Timeline**, not Plan. Plan mode renders a fixed task list
laid out up front; this agent has no upfront plan to show - it decides what
to do one tool call at a time as the model reasons, discovering its steps as
it goes rather than announcing them first. Timeline mode - individual steps
appearing as they happen - is what that shape of work actually looks like,
and it's Slack's own documented default for exactly this case.

Token-level streaming is deliberately not attempted. The MiniMax completion
this codebase calls (`agent/model.py`'s `Agent._complete`) is a single
non-streaming `chat.completions.create()`; there is no per-token feed to
relay, and the task brief is explicit that hammering `chat.appendStream` on
every token would be the wrong call anyway given Slack's rate limits. What
streams live is the real signal as it happens: reasoning as the model
produces it, and a task card per tool call, in_progress then complete/error.
The final answer text is attached to the single `chat.stopStream` call that
closes the message, which is what "append as the answer forms, then close
it" means with a non-streaming completion API underneath.

Fallback contract: a Slack API surface that isn't available - a plan-tier
gate, a missing scope, `channel_type_not_supported`, a timeout, anything
undocumented - must never cost the owner their answer. Every public method
here is best-effort: on any failure it marks itself inactive and every
later call becomes a fast no-op, so `agent/slack_app.py` falls back to
posting the final answer as one ordinary message, exactly as it did before
this module existed. The first failure is logged; later ones on the same
stream are not, per the brief's "log the reason once, not on every message".
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol

from agent.tools.base import REGISTRY

logger = logging.getLogger(__name__)

FEATURE_ENV = "SLACK_THINKING_STEPS"

# timeline: steps appear as they're discovered. plan: a fixed list shown
# upfront. See the module docstring for why timeline is the right choice
# here - this agent never has an upfront plan to show.
DISPLAY_MODE = "timeline"

# Wall-clock budget for a single Slack streaming API call. The tool loop
# awaits these inline (see agent/model.py's StepHook calls), so this bounds
# how much latency one slow/rate-limited Slack call can add to a reply -
# once, since a timeout also marks the stream inactive and every later call
# short-circuits (see ThinkingStream._call). Generous enough that an
# ordinary slow response doesn't trip it, short enough that a hung request
# can't meaningfully delay the reply.
STREAM_TIMEOUT_S = 6.0

# chat.startStream/appendStream/stopStream cap markdown_text at 12,000
# characters combined; these are per-field caps this module applies to its
# own chunks (reasoning text, a task's details/output) so one oversized tool
# result or reasoning block can't blow that budget on its own.
MAX_REASONING_CHARS = 3000
MAX_TASK_DETAIL_CHARS = 1500

MAX_TITLE_CHARS = 72

# task_ids only need to be unique within one streamed message - a plain
# per-stream counter is enough (see ThinkingStream.__init__).
_REASONING_TASK_PREFIX = "reasoning-"
_TOOL_TASK_PREFIX = "tool-"

_CLAUSE_MARKERS = (" - ", " -- ", ": ")
_SENTENCE_END = re.compile(r"\.(?:\s+[A-Z]|\s*$)")

# Imperative verbs this codebase's tool descriptions actually start with
# (confirmed by reading every agent/tools/*.py @tool() call), plus a few
# obvious siblings for tools added later. Not a tool-name -> title mapping -
# a mapping like that drifts the moment a description's wording changes.
# This is closed-class English morphology: gerund-ize the verb if we
# recognise it as one, otherwise leave the clause as written rather than
# risk mangling a description that doesn't open with a verb at all (see
# `_gerund_phrase`'s docstring).
_KNOWN_VERBS = frozenset(
    {
        "append", "add", "attach", "backup", "bring", "cancel", "capture",
        "check", "commit", "confirm", "copy", "delete", "download", "fetch",
        "get", "inspect", "list", "look", "mail", "move", "notify", "ping",
        "post", "query", "read", "reboot", "record", "remove", "report",
        "request", "resolve", "restart", "restore", "run", "safely",
        "schedule", "search", "send", "set", "smart-search", "snapshot",
        "start", "stop", "summarize", "sync", "take", "update", "verify",
        "write",
    }
)

_VOWELS = frozenset("aeiou")


def _gerund(word: str) -> str:
    """Best-effort imperative-verb-to-gerund, e.g. "Search" -> "Searching",
    "Take" -> "Taking", "Start" -> "Starting". Not a general English
    conjugator - it only has to be right for the verbs this codebase's tool
    descriptions actually use (`_KNOWN_VERBS`), which `_gerund_phrase` gates
    on before ever calling this."""
    if "-" in word:
        prefix, _, tail = word.rpartition("-")
        return f"{prefix}-{_gerund(tail)}"
    lower = word.lower()
    if lower.endswith("e") and not lower.endswith("ee"):
        stem = word[:-1]
    elif (
        len(lower) >= 3
        and lower[-1] not in _VOWELS
        and lower[-2] in _VOWELS
        and lower[-3] not in _VOWELS
        and lower[-1] not in "wxy"
    ):
        stem = word + word[-1]
    else:
        stem = word
    return stem + "ing"


def _leading_clause(description: str) -> str:
    """The first self-contained idea in a tool description: up to the first
    clause marker (a dash aside, a colon), or the first real sentence break
    if there's no marker. A plain `.` split would cut "e.g." in half, so the
    sentence check requires the period to be followed by a capital letter or
    the end of the string."""
    text = description.strip()
    for marker in _CLAUSE_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            return text[:idx].rstrip(" .")
    m = _SENTENCE_END.search(text)
    if m:
        return text[: m.start() + 1].rstrip(" .")
    return text.rstrip(" .")


def _title_case_first(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def task_title(tool_name: str) -> str:
    """A plain-language task-card title for `tool_name`, non-technical
    reader legible, derived from the tool's own registered description -
    see the module docstring and `_leading_clause`/`_gerund` for how, and
    why this isn't a second name -> title table.

    Always returns something reasonable, including for a tool name the
    registry doesn't know (the model can call a name that doesn't exist;
    the task card still needs a title, not a crash) and for a description
    that doesn't open with a recognised verb (left as written rather than
    risking a mangled gerund - see `_KNOWN_VERBS`).
    """
    fallback = _title_case_first(tool_name.replace("_", " ").strip()) or "Running a tool"
    entry = REGISTRY.get(tool_name)
    if entry is None or not entry.description:
        return fallback
    clause = _leading_clause(entry.description)
    if not clause:
        return fallback
    parts = clause.split(" ", 1)
    head = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    head_key = head.lower().rstrip(",;:")
    title = f"{_gerund(head)} {rest}".strip() if head_key in _KNOWN_VERBS else clause
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(",;:- ")
    return _title_case_first(title) or fallback


def enabled() -> bool:
    """Whether Thinking Steps is turned on for this process. Read at call
    time (like every other env-backed setting in this codebase - see
    `agent/slack_app.py`'s `reply_without_mention`), defaulting to on, so
    `SLACK_THINKING_STEPS=0` in the env file turns it off without a
    redeploy."""
    import os

    return os.environ.get(FEATURE_ENV, "1") != "0"


class StreamClient(Protocol):
    """The slice of the real Slack `AsyncWebClient` this module calls.
    `app.client`, passed through from `agent/slack_app.py`'s real listener,
    satisfies this structurally; a test double only needs these three
    methods (see tests/agent/test_slack_thinking.py)."""

    async def chat_startStream(self, **kwargs: Any) -> Any: ...

    async def chat_appendStream(self, **kwargs: Any) -> Any: ...

    async def chat_stopStream(self, **kwargs: Any) -> Any: ...

    async def auth_test(self, **kwargs: Any) -> Any: ...


_warned = False


def _warn_once(reason: str) -> None:
    """Log why Thinking Steps stopped working for this process, exactly
    once - not on every message, per the task brief. A later, different
    failure doesn't get its own log line either; the first one already told
    the operator this process isn't streaming and why, and that's the
    actionable fact."""
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning("Slack Thinking Steps disabled for this process: %s", reason)


def _get(resp: Any, key: str) -> Any:
    """`AsyncSlackResponse` behaves like a Mapping (subscriptable, `.get()`)
    without structurally satisfying `Mapping` under mypy --strict - same
    mismatch `agent/slack_app.py`'s `ConversationsClient` already works
    around. A plain dict (what every test double here returns) has `.get()`
    natively, so this just calls it either way."""
    getter = getattr(resp, "get", None)
    if getter is None:
        return None
    return getter(key)


_team_id_cache: str | None = None


async def _team_id(client: StreamClient) -> str | None:
    """The workspace's own team id, needed as `recipient_team_id` for every
    stream started outside a DM (see `ThinkingStream.start`). Fetched once
    per process and cached - a team's id doesn't change under a running
    process, the same reasoning `agent/tools/comms.py` applies to channel
    and user lookups. Returns `None` on any failure; the caller treats a
    missing team id as "can't stream this one", not as an error to raise.
    """
    global _team_id_cache
    if _team_id_cache is not None:
        return _team_id_cache
    try:
        resp = await asyncio.wait_for(client.auth_test(), STREAM_TIMEOUT_S)
    except Exception:
        return None
    if not _get(resp, "ok"):
        return None
    team_id = _get(resp, "team_id")
    if not isinstance(team_id, str) or not team_id:
        return None
    _team_id_cache = team_id
    return team_id


class ThinkingStream:
    """One streamed Slack message: the live view for a single reply.

    Structurally implements `agent.model.StepHook` (`reasoning`,
    `tool_started`, `tool_finished`) so `agent/slack_app.py` can hand it
    straight to `agent.model.use_step_hook` - see that module's docstring
    for why the hook is attached per-call via a ContextVar rather than
    stored on `Agent` itself.

    Every method is best-effort: `start()` returns whether streaming is
    actually live, and every other method silently no-ops once `active` is
    `False` (whether because `start()` never succeeded, or a later call
    failed) - see the module docstring's fallback contract.
    """

    def __init__(
        self,
        client: StreamClient,
        *,
        channel: str,
        thread_ts: str,
        recipient_user_id: str | None,
        recipient_team_id: str | None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._recipient_user_id = recipient_user_id
        self._recipient_team_id = recipient_team_id
        self._ts: str | None = None
        self._active = False
        self._counter = 0
        self._current_task_id: str | None = None

    @property
    def active(self) -> bool:
        return self._active

    def _next_task_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    async def start(self) -> bool:
        is_dm = self._channel.startswith("D")
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "thread_ts": self._thread_ts,
            "task_display_mode": DISPLAY_MODE,
        }
        if not is_dm:
            # Documented requirement: outside a DM, both recipient ids are
            # required - the detail the task brief flags as most often
            # missed. Neither being available (no Slack user id on the
            # event, or the team id lookup failing) is a fallback case, not
            # an error: this reply just doesn't stream.
            if not self._recipient_user_id or not self._recipient_team_id:
                _warn_once(
                    "recipient_user_id/recipient_team_id are required outside a DM "
                    "but weren't available"
                )
                return False
            kwargs["recipient_user_id"] = self._recipient_user_id
            kwargs["recipient_team_id"] = self._recipient_team_id
        try:
            resp = await asyncio.wait_for(
                self._client.chat_startStream(**kwargs), STREAM_TIMEOUT_S
            )
        except Exception as exc:
            _warn_once(f"chat.startStream raised: {exc!r}")
            return False
        if not _get(resp, "ok"):
            _warn_once(f"chat.startStream returned error={_get(resp, 'error')!r}")
            return False
        ts = _get(resp, "ts")
        if not isinstance(ts, str) or not ts:
            _warn_once("chat.startStream returned ok without a usable ts")
            return False
        self._ts = ts
        self._active = True
        return True

    async def _append(self, chunks: list[dict[str, Any]]) -> None:
        if not self._active or self._ts is None:
            return
        try:
            resp = await asyncio.wait_for(
                self._client.chat_appendStream(channel=self._channel, ts=self._ts, chunks=chunks),
                STREAM_TIMEOUT_S,
            )
        except Exception as exc:
            _warn_once(f"chat.appendStream raised: {exc!r}")
            self._active = False
            return
        if not _get(resp, "ok"):
            _warn_once(f"chat.appendStream returned error={_get(resp, 'error')!r}")
            self._active = False

    async def reasoning(self, text: str) -> None:
        """StepHook.reasoning: the model's chain-of-thought, which
        `agent/model.py` strips from the final answer, surfaces here
        instead - one task card per reasoning burst, titled plainly, its
        content in `output` rather than narrated as a chat message. This is
        the "detail behind the steps" the task brief asks for."""
        text = text.strip()
        if not text:
            return
        task_id = self._next_task_id(_REASONING_TASK_PREFIX)
        await self._append(
            [
                {
                    "type": "task_update",
                    "id": task_id,
                    "title": "Thinking it through",
                    "status": "complete",
                    "output": text[:MAX_REASONING_CHARS],
                }
            ]
        )

    async def tool_started(self, tool_name: str | None, args: dict[str, Any]) -> None:
        """StepHook.tool_started: a real tool call, not invented progress
        text - see the task brief. `tool_name` is `None` only for the
        unsupported-tool-call-type edge case `agent/model.py` already
        handles defensively; it still gets a card, just a generic one."""
        title = task_title(tool_name) if tool_name else "Running a tool call"
        task_id = self._next_task_id(_TOOL_TASK_PREFIX)
        self._current_task_id = task_id
        await self._append(
            [{"type": "task_update", "id": task_id, "title": title, "status": "in_progress"}]
        )

    async def tool_finished(
        self, tool_name: str | None, args: dict[str, Any], out: dict[str, Any]
    ) -> None:
        """StepHook.tool_finished: closes the card `tool_started` opened.
        Tool calls in this loop are strictly sequential (see
        `Agent._complete`'s `for raw_call in msg.tool_calls` - each call is
        awaited before the next begins), so the id `tool_started` just
        minted is always the right one to close here."""
        task_id = self._current_task_id
        if task_id is None:
            # Defensive only: tool_finished without a preceding
            # tool_started shouldn't happen given the sequential loop above,
            # but a stray call must still produce a valid card, not a
            # KeyError-shaped bug.
            task_id = self._next_task_id(_TOOL_TASK_PREFIX)
        self._current_task_id = None
        title = task_title(tool_name) if tool_name else "Running a tool call"
        ok = bool(out.get("ok"))
        chunk: dict[str, Any] = {
            "type": "task_update",
            "id": task_id,
            "title": title,
            "status": "complete" if ok else "error",
        }
        detail = str(out.get("result") if ok else out.get("error") or "")
        if detail:
            chunk["output" if ok else "details"] = detail[:MAX_TASK_DETAIL_CHARS]
        await self._append([chunk])

    async def stop(self, final_text: str) -> bool:
        """Close the stream with the finished answer. Slack limits
        `markdown_text` to 12,000 characters; this codebase's replies are
        chat answers, not documents, so truncation here is a last-resort
        safety net, not an expected path."""
        if not self._active or self._ts is None:
            return False
        text = final_text if final_text.strip() else " "
        try:
            resp = await asyncio.wait_for(
                self._client.chat_stopStream(
                    channel=self._channel, ts=self._ts, markdown_text=text[:12_000]
                ),
                STREAM_TIMEOUT_S,
            )
        except Exception as exc:
            _warn_once(f"chat.stopStream raised: {exc!r}")
            self._active = False
            return False
        if not _get(resp, "ok"):
            _warn_once(f"chat.stopStream returned error={_get(resp, 'error')!r}")
            self._active = False
            return False
        return True


async def build(
    client: StreamClient, *, channel: str, thread_ts: str, user: str | None
) -> ThinkingStream | None:
    """Construct and start a `ThinkingStream` for one reply, or return
    `None` if it isn't going to work - feature flag off, the client doesn't
    look like a real Slack client, or `start()` itself failed for any
    reason. `agent/slack_app.py`'s `handle_message` treats `None` as "post
    the final answer the old way", never as an error to surface.
    """
    if not enabled():
        return None
    # A test double for the plain reactions-only path (every pre-existing
    # test in tests/agent/test_slack.py) has no streaming methods at all -
    # that's simply "this client can't stream", not a bug to log about.
    for name in ("chat_startStream", "chat_appendStream", "chat_stopStream"):
        if not callable(getattr(client, name, None)):
            return None
    is_dm = channel.startswith("D")
    recipient_team_id = None if is_dm else await _team_id(client)
    stream = ThinkingStream(
        client,
        channel=channel,
        thread_ts=thread_ts,
        recipient_user_id=user,
        recipient_team_id=recipient_team_id,
    )
    started = await stream.start()
    return stream if started else None
