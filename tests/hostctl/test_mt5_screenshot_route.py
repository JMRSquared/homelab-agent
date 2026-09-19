import pytest
from fastapi.testclient import TestClient

from hostctl import screendump
from hostctl.app import app

AUTH = {"Authorization": "Bearer testtoken"}


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("HOSTCTL_TOKEN", "testtoken")


def test_screenshot_route_returns_png_bytes(monkeypatch):
    monkeypatch.setattr(screendump, "capture_png", lambda vmid=200: b"\x89PNGfakebytes")
    r = TestClient(app).post("/mt5/screenshot", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == b"\x89PNGfakebytes"


def test_screenshot_route_maps_timeout_to_504(monkeypatch):
    def _boom(vmid=200):
        raise screendump.ScreendumpTimeoutError("no ppm ever appeared")

    monkeypatch.setattr(screendump, "capture_png", _boom)
    r = TestClient(app).post("/mt5/screenshot", headers=AUTH)
    assert r.status_code == 504
    assert "no ppm ever appeared" in r.json()["detail"]


def test_screenshot_route_maps_conversion_failure_to_422(monkeypatch):
    def _boom(vmid=200):
        raise screendump.ScreendumpConversionError("ffmpeg blew up")

    monkeypatch.setattr(screendump, "capture_png", _boom)
    r = TestClient(app).post("/mt5/screenshot", headers=AUTH)
    assert r.status_code == 422
    assert "ffmpeg blew up" in r.json()["detail"]


def test_screenshot_route_requires_auth():
    r = TestClient(app).post("/mt5/screenshot")
    assert r.status_code == 401
