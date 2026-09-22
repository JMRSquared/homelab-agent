import httpx
import pytest
import respx

from agent.tools import base, comms  # noqa: F401

SLACK = "https://slack.com/api"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    # Every cache is module-level and process-lifetime by design (see
    # comms.py) - reset between tests so one test's channel/user lookups
    # don't leak into the next and hide a missing API call.
    comms._channel_ids.clear()
    comms._user_names.clear()
    comms._channel_info.clear()


def _list_response(channels):
    return httpx.Response(200, json={"ok": True, "channels": channels, "response_metadata": {}})


@respx.mock
def test_slack_history_resolves_channel_name_and_returns_messages():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    respx.get(f"{SLACK}/users.info").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "user": {"id": "U1", "real_name": "Tech"}}
        )
    )
    history = respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "status update", "ts": "1700000000.000100"}
                ],
                "has_more": False,
            },
        )
    )
    out = base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    assert out["ok"] is True
    result = out["result"]
    assert result["channel_id"] == "C123"
    assert result["messages"] == [
        {
            "author": "Tech",
            "text": "status update",
            "ts": "1700000000.000100",
            "is_bot": False,
            "thread_ts": None,
        }
    ]
    assert result["truncated"] is False
    assert history.calls.last.request.url.params["channel"] == "C123"


@respx.mock
def test_slack_history_caches_channel_lookup_across_calls():
    list_route = respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    assert list_route.call_count == 1


@respx.mock
def test_slack_history_passes_through_a_raw_channel_id_without_lookup():
    list_route = respx.get(f"{SLACK}/conversations.list")
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": True, "messages": [], "has_more": False})
    )
    out = base.dispatch("slack_history", {"channel": "C0123456789"})
    assert out["ok"] is True
    assert not list_route.called


@respx.mock
def test_slack_history_unknown_channel_name_fails_cleanly():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    history = respx.get(f"{SLACK}/conversations.history")
    out = base.dispatch("slack_history", {"channel": "#no-such-channel"})
    assert out["ok"] is False
    assert "no-such-channel" in out["error"]
    assert not history.called


@respx.mock
def test_slack_history_channel_bot_not_in_fails_cleanly():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C999", "name": "private-room"}])
    )
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
    )
    out = base.dispatch("slack_history", {"channel": "#private-room"})
    assert out["ok"] is False
    assert "not_in_channel" in out["error"]


@respx.mock
def test_slack_history_caps_total_text_and_reports_truncation():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    respx.get(f"{SLACK}/users.info").mock(
        return_value=httpx.Response(200, json={"ok": True, "user": {"id": "U1", "name": "u1"}})
    )
    big = "x" * (comms._TEXT_CAP - 100)
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": big, "ts": "1.1"},
                    {"user": "U1", "text": "y" * 500, "ts": "2.2"},
                ],
                "has_more": False,
            },
        )
    )
    out = base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    result = out["result"]
    assert len(result["messages"]) == 1
    assert result["truncated"] is True


@respx.mock
def test_slack_history_bot_message_uses_username_not_users_info():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    users_info = respx.get(f"{SLACK}/users.info")
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {
                        "bot_id": "B1",
                        "username": "TazzieBot",
                        "text": "hourly report",
                        "ts": "1.1",
                    }
                ],
                "has_more": False,
            },
        )
    )
    out = base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    assert out["result"]["messages"][0]["author"] == "TazzieBot"
    assert out["result"]["messages"][0]["is_bot"] is True
    assert not users_info.called


@respx.mock
def test_slack_thread_replies_reaches_conversations_replies():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    respx.get(f"{SLACK}/users.info").mock(
        return_value=httpx.Response(200, json={"ok": True, "user": {"id": "U1", "name": "u1"}})
    )
    replies = respx.get(f"{SLACK}/conversations.replies").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"user": "U1", "text": "reply one", "ts": "1700000000.000200"}],
                "has_more": False,
            },
        )
    )
    out = base.dispatch(
        "slack_thread_replies",
        {"channel": "#homelab-mt5", "thread_ts": "1700000000.000100"},
    )
    assert out["ok"] is True
    assert out["result"]["messages"][0]["text"] == "reply one"
    assert replies.calls.last.request.url.params["ts"] == "1700000000.000100"


@respx.mock
def test_user_name_lookup_is_cached_across_messages():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mt5"}])
    )
    users_info = respx.get(f"{SLACK}/users.info").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "user": {"id": "U1", "real_name": "Tech"}}
        )
    )
    respx.get(f"{SLACK}/conversations.history").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [
                    {"user": "U1", "text": "one", "ts": "1.1"},
                    {"user": "U1", "text": "two", "ts": "2.2"},
                ],
                "has_more": False,
            },
        )
    )
    base.dispatch("slack_history", {"channel": "#homelab-mt5"})
    assert users_info.call_count == 1


@respx.mock
def test_channel_info_returns_name_topic_and_purpose():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-income"}])
    )
    respx.get(f"{SLACK}/conversations.info").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channel": {
                    "name": "homelab-income",
                    "topic": {"value": "money stuff"},
                    "purpose": {"value": "Autonomous passive-income earnings."},
                },
            },
        )
    )
    info = comms.channel_info("#homelab-income")
    assert info == {
        "name": "homelab-income",
        "topic": "money stuff",
        "purpose": "Autonomous passive-income earnings.",
    }


@respx.mock
def test_channel_info_handles_no_topic_or_purpose():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C123", "name": "homelab-mail"}])
    )
    respx.get(f"{SLACK}/conversations.info").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channel": {
                    "name": "homelab-mail",
                    "topic": {"value": ""},
                    "purpose": {"value": ""},
                },
            },
        )
    )
    info = comms.channel_info("#homelab-mail")
    assert info["topic"] == ""
    assert info["purpose"] == ""
    assert info["name"] == "homelab-mail"


@respx.mock
def test_channel_info_is_cached_across_calls():
    respx.get(f"{SLACK}/conversations.list").mock(
        return_value=_list_response([{"id": "C0123456789", "name": "homelab-mt5"}])
    )
    info_route = respx.get(f"{SLACK}/conversations.info").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "channel": {
                    "name": "homelab-mt5",
                    "topic": {"value": ""},
                    "purpose": {"value": ""},
                },
            },
        )
    )
    comms.channel_info("#homelab-mt5")
    comms.channel_info("#homelab-mt5")
    comms.channel_info("C0123456789")
    assert info_route.call_count == 1


@respx.mock
def test_channel_info_dm_falls_back_to_direct_message_name():
    respx.get(f"{SLACK}/conversations.info").mock(
        return_value=httpx.Response(200, json={"ok": True, "channel": {}})
    )
    info = comms.channel_info("D0123456789")
    assert info["name"] == "direct message"
    assert info["topic"] == ""
    assert info["purpose"] == ""
