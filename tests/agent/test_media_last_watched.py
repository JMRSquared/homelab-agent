import httpx
import pytest
import respx

from agent.tools import base, media  # noqa: F401

JELLYFIN = "http://10.0.0.165:8096"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("JELLYFIN_KEY", "jkey")


@respx.mock
def test_merges_across_every_user_and_takes_the_most_recent():
    """Regression test for the exact live defect: watch history sits under
    `root`, not the account you'd guess (`Kamo`) - iterating every user and
    merging is the whole point of this tool."""
    respx.get(f"{JELLYFIN}/Users").mock(
        return_value=httpx.Response(
            200, json=[{"Id": "kamo-id", "Name": "Kamo"}, {"Id": "root-id", "Name": "root"}]
        )
    )
    respx.get(f"{JELLYFIN}/Users/kamo-id/Items").mock(
        return_value=httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Name": "Old Movie",
                        "ProductionYear": 2010,
                        "RunTimeTicks": 60_000_000_000,
                        "UserData": {"LastPlayedDate": "2026-01-01T00:00:00Z"},
                    }
                ]
            },
        )
    )
    respx.get(f"{JELLYFIN}/Users/root-id/Items").mock(
        return_value=httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Name": "Mutiny",
                        "ProductionYear": 2026,
                        "RunTimeTicks": 57_000_000_000,
                        "UserData": {"LastPlayedDate": "2026-09-16T17:35:39Z"},
                    }
                ]
            },
        )
    )
    out = base.dispatch("media_last_watched", {})
    assert out["ok"] is True
    item = out["result"]["items"][0]
    assert item["title"] == "Mutiny"
    assert item["year"] == 2026
    assert item["runtime_minutes"] == 95
    assert item["watched_at"] == "2026-09-16T17:35:39Z"
    assert item["watched_by"] == "root"


@respx.mock
def test_no_history_for_any_user_is_a_clear_empty_result():
    respx.get(f"{JELLYFIN}/Users").mock(
        return_value=httpx.Response(200, json=[{"Id": "root-id", "Name": "root"}])
    )
    respx.get(f"{JELLYFIN}/Users/root-id/Items").mock(
        return_value=httpx.Response(200, json={"Items": []})
    )
    out = base.dispatch("media_last_watched", {})
    assert out["ok"] is True
    assert out["result"]["items"] == []
    assert "no watch history" in out["result"]["note"]


@respx.mock
def test_count_returns_multiple_items():
    respx.get(f"{JELLYFIN}/Users").mock(
        return_value=httpx.Response(200, json=[{"Id": "root-id", "Name": "root"}])
    )
    respx.get(f"{JELLYFIN}/Users/root-id/Items").mock(
        return_value=httpx.Response(
            200,
            json={
                "Items": [
                    {
                        "Name": "Mutiny",
                        "ProductionYear": 2026,
                        "RunTimeTicks": 57_000_000_000,
                        "UserData": {"LastPlayedDate": "2026-09-16T17:35:39Z"},
                    },
                    {
                        "Name": "Earlier Film",
                        "ProductionYear": 2020,
                        "RunTimeTicks": 60_000_000_000,
                        "UserData": {"LastPlayedDate": "2026-09-01T00:00:00Z"},
                    },
                ]
            },
        )
    )
    out = base.dispatch("media_last_watched", {"count": 2})
    assert out["ok"] is True
    assert [i["title"] for i in out["result"]["items"]] == ["Mutiny", "Earlier Film"]


def test_rejects_zero_count():
    with pytest.raises(ValueError):
        media.media_last_watched(0)
