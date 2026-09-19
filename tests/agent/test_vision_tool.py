from io import BytesIO

import pytest
from PIL import Image

from agent import clients
from agent.tools import base, outbox, vision  # noqa: F401


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, answer="looks like an RS3: honeycomb grille, RS3 badge", raise_exc=None):
        self.answer = answer
        self.raise_exc = raise_exc
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_exc:
            raise self.raise_exc
        return _FakeResponse(self.answer)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("MINIMAX_API_KEY", "mkey")


def _write_png(name="photo.png", size=(10, 10)):
    path = outbox.outbox_dir() / name
    img = Image.new("RGB", size, color=(10, 20, 30))
    img.save(path, format="PNG")
    return str(path)


def test_image_inspect_happy_path(monkeypatch):
    fake_completions = _FakeCompletions()
    monkeypatch.setattr(clients, "minimax_client", lambda: _FakeClient(fake_completions))
    path = _write_png()

    out = base.dispatch("image_inspect", {"path": path, "question": "is this an RS3?"})
    assert out["ok"] is True
    result = out["result"]
    assert result["answer"] == fake_completions.answer
    assert result["downscaled"] is False
    assert result["path"] == path

    sent_content = fake_completions.last_kwargs["messages"][0]["content"]
    assert sent_content[0] == {"type": "text", "text": "is this an RS3?"}
    assert sent_content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_inspect_rejects_path_outside_outbox(tmp_path, monkeypatch):
    outside = tmp_path / "not_outbox.png"
    Image.new("RGB", (5, 5)).save(outside, format="PNG")
    fake_completions = _FakeCompletions()
    monkeypatch.setattr(clients, "minimax_client", lambda: _FakeClient(fake_completions))

    out = base.dispatch("image_inspect", {"path": str(outside), "question": "what is this?"})
    assert out["ok"] is False
    assert "outbox" in out["error"]
    assert fake_completions.last_kwargs is None


def test_image_inspect_rejects_missing_file():
    out = base.dispatch(
        "image_inspect",
        {"path": "/tank/dev/agent/outbox/nope.png", "question": "what is this?"},
    )
    assert out["ok"] is False


def test_image_inspect_rejects_non_image_file():
    path = outbox.outbox_dir() / "notes.txt"
    path.write_text("just some text, not an image")
    out = base.dispatch("image_inspect", {"path": str(path), "question": "what is this?"})
    assert out["ok"] is False
    assert "image" in out["error"]


def test_image_inspect_rejects_blank_question():
    path = _write_png()
    out = base.dispatch("image_inspect", {"path": path, "question": "   "})
    assert out["ok"] is False


def test_image_inspect_downscales_large_images(monkeypatch):
    monkeypatch.setattr(vision, "DOWNSCALE_ABOVE_BYTES", 100)
    fake_completions = _FakeCompletions()
    monkeypatch.setattr(clients, "minimax_client", lambda: _FakeClient(fake_completions))
    path = _write_png(size=(200, 200))  # well over 100 raw bytes once written as PNG

    out = base.dispatch("image_inspect", {"path": path, "question": "what color?"})
    assert out["ok"] is True
    assert out["result"]["downscaled"] is True
    sent_content = fake_completions.last_kwargs["messages"][0]["content"]
    # Downscaling always re-encodes to JPEG.
    assert sent_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_inspect_downscale_caps_the_longest_edge(monkeypatch):
    monkeypatch.setattr(vision, "DOWNSCALE_ABOVE_BYTES", 100)
    monkeypatch.setattr(vision, "MAX_DIMENSION", 50)
    data, content_type = vision._prepare_for_upload(
        BytesIO(_make_large_png()).getvalue(), "image/png"
    )
    assert content_type == "image/jpeg"
    with Image.open(BytesIO(data)) as img:
        assert max(img.size) <= 50


def _make_large_png() -> bytes:
    img = Image.new("RGB", (300, 200), color=(1, 2, 3))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_image_inspect_wraps_model_endpoint_errors(monkeypatch):
    fake_completions = _FakeCompletions(raise_exc=RuntimeError("400 bad image shape"))
    monkeypatch.setattr(clients, "minimax_client", lambda: _FakeClient(fake_completions))
    path = _write_png()

    out = base.dispatch("image_inspect", {"path": path, "question": "what is this?"})
    assert out["ok"] is False
    assert "rejected" in out["error"]
