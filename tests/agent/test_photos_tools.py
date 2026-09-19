import json

import httpx
import pytest
import respx

from agent.tools import base, photos  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("IMMICH_KEY", "ikey")


@respx.mock
def test_search_uses_smart_search_endpoint():
    route = respx.post("http://10.0.0.165:2283/api/search/smart").mock(
        return_value=httpx.Response(200, json={"assets": {"items": []}})
    )
    base.dispatch("photos_search", {"query": "the dog on the beach"})
    assert route.calls.last.request.headers["x-api-key"] == "ikey"
    assert json.loads(route.calls.last.request.read()) == {
        "query": "the dog on the beach",
        "size": 10,
    }


@respx.mock
def test_stats_reports_counts():
    respx.get("http://10.0.0.165:2283/api/server/statistics").mock(
        return_value=httpx.Response(200, json={"photos": 41233, "videos": 902})
    )
    out = base.dispatch("photos_stats", {})
    assert out["result"]["photos"] == 41233


def test_search_rejects_blank_query():
    assert base.dispatch("photos_search", {"query": "   "})["ok"] is False


def test_photos_search_rejects_direct_call_with_blank_query():
    with pytest.raises(ValueError):
        photos.photos_search("")
