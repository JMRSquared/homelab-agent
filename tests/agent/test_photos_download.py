import httpx
import pytest
import respx

from agent.tools import base, photos  # noqa: F401


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("IMMICH_KEY", "ikey")
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))


def _search_response(asset_id="abc123", filename="audi.tiff"):
    return {"assets": {"items": [{"id": asset_id, "originalFileName": filename}]}}


@respx.mock
def test_photos_download_defaults_to_preview_jpeg():
    respx.post("http://10.0.0.165:2283/api/search/smart").mock(
        return_value=httpx.Response(200, json=_search_response())
    )
    route = respx.get("http://10.0.0.165:2283/api/assets/abc123/thumbnail").mock(
        return_value=httpx.Response(
            200,
            content=b"\xff\xd8\xffnotreallyjpegbutcloseenough",
            headers={"content-type": "image/jpeg"},
        )
    )
    out = base.dispatch("photos_download", {"query": "black audi rs3"})
    assert out["ok"] is True
    result = out["result"]
    assert result["rendition"] == "preview_jpeg"
    assert result["path"].endswith(".jpg")
    assert result["content_type"] == "image/jpeg"
    assert route.calls.last.request.url.params["size"] == "preview"
    import pathlib

    assert pathlib.Path(result["path"]).read_bytes().startswith(b"\xff\xd8\xff")


@respx.mock
def test_photos_download_original_hits_original_endpoint():
    respx.post("http://10.0.0.165:2283/api/search/smart").mock(
        return_value=httpx.Response(200, json=_search_response(filename="audi.tiff"))
    )
    respx.get("http://10.0.0.165:2283/api/assets/abc123/original").mock(
        return_value=httpx.Response(
            200, content=b"II*\x00fake-tiff", headers={"content-type": "image/tiff"}
        )
    )
    out = base.dispatch("photos_download", {"query": "black audi rs3", "original": True})
    assert out["ok"] is True
    assert out["result"]["rendition"] == "original"
    assert out["result"]["path"].endswith(".tiff")


@respx.mock
def test_photos_download_no_hits_is_a_clear_error():
    respx.post("http://10.0.0.165:2283/api/search/smart").mock(
        return_value=httpx.Response(200, json={"assets": {"items": []}})
    )
    out = base.dispatch("photos_download", {"query": "nonexistent thing"})
    assert out["ok"] is False
    assert "no photos matched" in out["error"]


def test_photos_download_rejects_blank_query():
    out = base.dispatch("photos_download", {"query": "   "})
    assert out["ok"] is False


def test_photos_download_direct_call_raises_on_blank_query():
    with pytest.raises(ValueError):
        photos.photos_download("")
