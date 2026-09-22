import asyncio
from typing import Any

import pytest

from agent import slack_thinking
from agent.tools import base


@pytest.fixture(autouse=True)
def _clean_registry():
    snapshot = dict(base.REGISTRY)
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_warned_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_thinking, "_warned", False)


@pytest.fixture(autouse=True)
def _reset_team_id_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack_thinking, "_team_id_cache", None)


class FakeStreamClient:
    """Records every streaming call. `start_ok`/`append_ok`/`stop_ok` let a
    test make one call fail without touching the others - the fallback
    contract this module promises is per-call, not all-or-nothing."""

    def __init__(
        self,
        *,
        start_response: dict[str, Any] | None = None,
        append_response: dict[str, Any] | None = None,
        stop_response: dict[str, Any] | None = None,
        auth_response: dict[str, Any] | None = None,
        raise_on: frozenset[str] = frozenset(),
    ) -> None:
        self.start_response = start_response or {"ok": True, "ts": "500.0"}
        self.append_response = append_response or {"ok": True}
        self.stop_response = stop_response or {"ok": True}
        self.auth_response = auth_response or {"ok": True, "team_id": "T1"}
        self._raise_on = raise_on
        self.start_calls: list[dict[str, Any]] = []
        self.append_calls: list[dict[str, Any]] = []
        self.stop_calls: list[dict[str, Any]] = []

    async def chat_startStream(self, **kwargs: Any) -> dict[str, Any]:
        self.start_calls.append(kwargs)
        if "start" in self._raise_on:
            raise RuntimeError("boom")
        return self.start_response

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, Any]:
        self.append_calls.append(kwargs)
        if "append" in self._raise_on:
            raise RuntimeError("boom")
        return self.append_response

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, Any]:
        self.stop_calls.append(kwargs)
        if "stop" in self._raise_on:
            raise RuntimeError("boom")
        return self.stop_response

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        if "auth" in self._raise_on:
            raise RuntimeError("boom")
        return self.auth_response


# --- task_title --------------------------------------------------------


def test_task_title_gerund_izes_a_known_leading_verb() -> None:
    base.REGISTRY.clear()

    @base.tool(
        "photos_search",
        "Search the family photo library by describing what's in the picture "
        "(subject, place, people). Read-only smart search against Immich.",
        {"type": "object", "properties": {}},
    )
    def _search() -> dict:
        return {}

    title = slack_thinking.task_title("photos_search")
    assert title.startswith("Searching the family photo library")


def test_task_title_handles_hyphenated_leading_verb() -> None:
    base.REGISTRY.clear()

    @base.tool(
        "photos_download",
        "Smart-search the family photo library and download a matching image to "
        "the local outbox, ready to attach to an email or message - OR, given "
        "asset_id, download that exact asset directly.",
        {"type": "object", "properties": {}},
    )
    def _download() -> dict:
        return {}

    title = slack_thinking.task_title("photos_download")
    assert title.startswith("Smart-searching")
    assert len(title) <= slack_thinking.MAX_TITLE_CHARS


def test_task_title_falls_back_for_unknown_tool_name() -> None:
    base.REGISTRY.clear()
    assert slack_thinking.task_title("mystery_tool") == "Mystery tool"


def test_task_title_leaves_a_non_verb_description_untouched_rather_than_mangling_it() -> None:
    base.REGISTRY.clear()

    @base.tool("photos_stats", "Photo and video counts and storage used by Immich.", {})
    def _stats() -> dict:
        return {}

    title = slack_thinking.task_title("photos_stats")
    # Must not produce a nonsense gerund like "Photoing" - a description
    # that doesn't open with a recognised verb is left as written.
    assert "Photoing" not in title
    assert title.startswith("Photo and video counts")


def test_task_title_never_exceeds_max_chars_even_for_a_long_description() -> None:
    base.REGISTRY.clear()

    @base.tool(
        "guest_exec",
        "Run a shell command as root on any guest - LXC or the Windows trading "
        "VM (200/mt5) - the general-purpose way to reach anything the narrower "
        "tools above cannot.",
        {},
    )
    def _exec() -> dict:
        return {}

    title = slack_thinking.task_title("guest_exec")
    assert len(title) <= slack_thinking.MAX_TITLE_CHARS
    assert title.startswith("Running a shell command")


# --- enabled() -----------------------------------------------------------


def test_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(slack_thinking.FEATURE_ENV, raising=False)
    assert slack_thinking.enabled() is True


def test_enabled_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slack_thinking.FEATURE_ENV, "0")
    assert slack_thinking.enabled() is False


# --- build() ---------------------------------------------------------------


def test_build_returns_none_when_feature_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(slack_thinking.FEATURE_ENV, "0")
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is None
    assert client.start_calls == []


def test_build_returns_none_when_the_client_cannot_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(slack_thinking.FEATURE_ENV, raising=False)

    class NoStreamingClient:
        async def reactions_add(self, **kwargs: Any) -> None:
            return None

    stream = asyncio.run(
        slack_thinking.build(NoStreamingClient(), channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is None


def test_build_starts_a_dm_stream_without_recipient_ids() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    assert stream.active
    kwargs = client.start_calls[0]
    assert "recipient_user_id" not in kwargs
    assert "recipient_team_id" not in kwargs
    assert kwargs["task_display_mode"] == "timeline"


def test_build_fetches_team_id_and_passes_recipient_ids_outside_a_dm() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    kwargs = client.start_calls[0]
    assert kwargs["recipient_user_id"] == "U1"
    assert kwargs["recipient_team_id"] == "T1"


def test_build_falls_back_when_channel_type_not_supported() -> None:
    """The exact documented error the task brief calls out."""
    client = FakeStreamClient(start_response={"ok": False, "error": "channel_type_not_supported"})
    stream = asyncio.run(
        slack_thinking.build(client, channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is None


def test_build_falls_back_when_start_stream_raises() -> None:
    client = FakeStreamClient(raise_on=frozenset({"start"}))
    stream = asyncio.run(
        slack_thinking.build(client, channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is None


def test_build_falls_back_when_team_id_lookup_fails_outside_a_dm() -> None:
    client = FakeStreamClient(raise_on=frozenset({"auth"}))
    stream = asyncio.run(
        slack_thinking.build(client, channel="C1", thread_ts="1.1", user="U1")
    )
    assert stream is None
    assert client.start_calls == []


# --- ThinkingStream methods --------------------------------------------


def test_tool_started_then_finished_appends_in_progress_then_complete() -> None:
    base.REGISTRY.clear()

    @base.tool("noop", "Check nothing in particular.", {})
    def _noop() -> dict:
        return {}

    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None

    asyncio.run(stream.tool_started("noop", {}))
    asyncio.run(stream.tool_finished("noop", {}, {"ok": True, "result": "fine"}))

    statuses = [c["chunks"][0]["status"] for c in client.append_calls]
    assert statuses == ["in_progress", "complete"]
    assert client.append_calls[1]["chunks"][0]["id"] == client.append_calls[0]["chunks"][0]["id"]


def test_tool_finished_marks_a_failed_tool_as_error() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None

    asyncio.run(stream.tool_started("adguard_report", {}))
    asyncio.run(
        stream.tool_finished(
            "adguard_report", {}, {"ok": False, "error": "the AdGuard key isn't configured"}
        )
    )

    last_chunk = client.append_calls[-1]["chunks"][0]
    assert last_chunk["status"] == "error"
    assert "AdGuard" in last_chunk["details"]


def test_reasoning_becomes_a_task_card_not_a_chat_message() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None

    asyncio.run(stream.reasoning("the load looks fine so I'll just say so"))

    chunk = client.append_calls[0]["chunks"][0]
    assert chunk["type"] == "task_update"
    assert chunk["status"] == "complete"
    assert "load looks fine" in chunk["output"]


def test_reasoning_with_blank_text_appends_nothing() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    asyncio.run(stream.reasoning("   "))
    assert client.append_calls == []


def test_stop_closes_the_stream_with_the_final_text() -> None:
    client = FakeStreamClient()
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    ok = asyncio.run(stream.stop("all done"))
    assert ok is True
    assert client.stop_calls[0]["markdown_text"] == "all done"
    assert client.stop_calls[0]["ts"] == "500.0"


def test_a_failed_append_marks_the_stream_inactive_and_further_calls_no_op() -> None:
    client = FakeStreamClient(raise_on=frozenset({"append"}))
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    asyncio.run(stream.reasoning("first"))
    assert stream.active is False
    asyncio.run(stream.reasoning("second"))
    # Only the first append was attempted - the second no-ops immediately.
    assert len(client.append_calls) == 1


def test_stop_returns_false_and_never_raises_when_the_client_errors() -> None:
    client = FakeStreamClient(raise_on=frozenset({"stop"}))
    stream = asyncio.run(
        slack_thinking.build(client, channel="D1", thread_ts="1.1", user="U1")
    )
    assert stream is not None
    ok = asyncio.run(stream.stop("done"))
    assert ok is False


def test_warn_once_logs_a_single_line_for_repeated_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        slack_thinking._warn_once("first reason")
        slack_thinking._warn_once("second reason")
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "first reason" in warnings[0].message
