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


def test_photos_download_returns_candidates_and_a_confidence_note():
    """Regression test for the S3-vs-RS3 defect: the result must carry
    enough for the model to notice a wrong match and self-correct, not
    just the single downloaded file."""
    with respx.mock:
        respx.post("http://10.0.0.165:2283/api/search/smart").mock(
            return_value=httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [
                            {"id": "s3-id", "originalFileName": "audi_s3.jpg"},
                            {"id": "other-id", "originalFileName": "other.jpg"},
                            {"id": "rs3-id", "originalFileName": "audi_rs3.jpg"},
                        ]
                    }
                },
            )
        )
        respx.get("http://10.0.0.165:2283/api/assets/s3-id/thumbnail").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8\xffjpeg", headers={"content-type": "image/jpeg"}
            )
        )
        out = base.dispatch("photos_download", {"query": "black audi rs3"})
        assert out["ok"] is True
        result = out["result"]
        assert result["asset_id"] == "s3-id"
        assert result["candidates"] == [
            {"index": 0, "asset_id": "s3-id", "filename": "audi_s3.jpg"},
            {"index": 1, "asset_id": "other-id", "filename": "other.jpg"},
            {"index": 2, "asset_id": "rs3-id", "filename": "audi_rs3.jpg"},
        ]
        assert "similarity" in result["note"]


def test_photos_download_index_selects_a_different_candidate():
    with respx.mock:
        respx.post("http://10.0.0.165:2283/api/search/smart").mock(
            return_value=httpx.Response(
                200,
                json={
                    "assets": {
                        "items": [
                            {"id": "s3-id", "originalFileName": "audi_s3.jpg"},
                            {"id": "other-id", "originalFileName": "other.jpg"},
                            {"id": "rs3-id", "originalFileName": "audi_rs3.jpg"},
                        ]
                    }
                },
            )
        )
        respx.get("http://10.0.0.165:2283/api/assets/rs3-id/thumbnail").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8\xffjpeg", headers={"content-type": "image/jpeg"}
            )
        )
        out = base.dispatch("photos_download", {"query": "black audi rs3", "index": 2})
        assert out["ok"] is True
        assert out["result"]["asset_id"] == "rs3-id"


def test_photos_download_asset_id_skips_search():
    with respx.mock:
        search_route = respx.post("http://10.0.0.165:2283/api/search/smart")
        respx.get("http://10.0.0.165:2283/api/assets/rs3-id").mock(
            return_value=httpx.Response(200, json={"originalFileName": "audi_rs3.jpg"})
        )
        respx.get("http://10.0.0.165:2283/api/assets/rs3-id/thumbnail").mock(
            return_value=httpx.Response(
                200, content=b"\xff\xd8\xffjpeg", headers={"content-type": "image/jpeg"}
            )
        )
        out = base.dispatch("photos_download", {"asset_id": "rs3-id"})
        assert out["ok"] is True
        assert out["result"]["asset_id"] == "rs3-id"
        assert out["result"]["candidates"] == []
        assert not search_route.called


def test_photos_download_rejects_both_query_and_asset_id():
    out = base.dispatch("photos_download", {"query": "x", "asset_id": "y"})
    assert out["ok"] is False


def test_photos_download_rejects_neither_query_nor_asset_id():
    out = base.dispatch("photos_download", {})
    assert out["ok"] is False


def test_photos_download_rejects_out_of_range_index():
    with respx.mock:
        respx.post("http://10.0.0.165:2283/api/search/smart").mock(
            return_value=httpx.Response(200, json=_search_response())
        )
        out = base.dispatch("photos_download", {"query": "audi", "index": 5})
        assert out["ok"] is False
        assert "index" in out["error"]
