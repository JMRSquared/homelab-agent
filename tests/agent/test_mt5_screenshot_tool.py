import pathlib

import httpx
import pytest
import respx

from agent.tools import base, mt5_screenshot  # noqa: F401

AGENT_HOSTCTL = "http://10.0.0.2:8710"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOSTCTL_URL", AGENT_HOSTCTL)
    monkeypatch.setenv("HOSTCTL_TOKEN", "t")
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))


@respx.mock
def test_mt5_screenshot_saves_png_to_outbox():
    png_bytes = b"\x89PNG\r\n\x1a\n" + bytes(20)
    respx.post(f"{AGENT_HOSTCTL}/mt5/screenshot").mock(
        return_value=httpx.Response(200, content=png_bytes, headers={"content-type": "image/png"})
    )
    out = base.dispatch("mt5_screenshot", {})
    assert out["ok"] is True
    result = out["result"]
    assert result["path"].endswith(".png")
    assert result["byte_size"] == len(png_bytes)
    assert pathlib.Path(result["path"]).read_bytes() == png_bytes


@respx.mock
def test_mt5_screenshot_propagates_hostctl_error_as_typed_failure():
    respx.post(f"{AGENT_HOSTCTL}/mt5/screenshot").mock(
        return_value=httpx.Response(504, json={"detail": "timed out"})
    )
    out = base.dispatch("mt5_screenshot", {})
    assert out["ok"] is False
