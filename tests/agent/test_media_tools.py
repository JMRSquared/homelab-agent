import json

import httpx
import pytest
import respx

from agent.tools import base, media  # noqa: F401


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JELLYFIN_KEY", "jkey")
    monkeypatch.setenv("JELLYSEERR_KEY", "skey")


@respx.mock
def test_jellyfin_uses_mediabrowser_auth_header():
    route = respx.get("http://10.0.0.165:8096/Items").mock(
        return_value=httpx.Response(200, json={"Items": []})
    )
    base.dispatch("media_search", {"query": "dune"})
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == 'MediaBrowser Token="jkey"'


@respx.mock
def test_request_posts_to_jellyseerr_with_media_type():
    route = respx.post("http://10.0.0.165:5055/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 7})
    )
    respx.get("http://10.0.0.165:5055/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 42, "mediaType": "movie"}]})
    )
    out = base.dispatch("media_request", {"query": "dune", "kind": "movie"})
    assert out["ok"] is True
    assert json.loads(route.calls.last.request.read()) == {"mediaId": 42, "mediaType": "movie"}


def test_request_rejects_unknown_kind():
    assert base.dispatch("media_request", {"query": "x", "kind": "album"})["ok"] is False


@respx.mock
def test_request_reports_no_match_when_kind_mismatches():
    respx.get("http://10.0.0.165:5055/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 42, "mediaType": "tv"}]})
    )
    out = base.dispatch("media_request", {"query": "dune", "kind": "show"})
    assert out["ok"] is True
    assert out["result"]["requested"] is False
    assert "dune" in out["result"]["reason"]


@respx.mock
def test_request_reports_no_match_when_search_is_empty():
    respx.get("http://10.0.0.165:5055/api/v1/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    out = base.dispatch("media_request", {"query": "nonexistent film", "kind": "movie"})
    assert out["ok"] is True
    assert out["result"]["requested"] is False


def test_media_search_rejects_direct_call_with_blank_query():
    with pytest.raises(ValueError):
        media.media_search("   ")


def test_media_request_rejects_direct_call_with_bad_kind():
    with pytest.raises(ValueError):
        media.media_request("dune", "album")
