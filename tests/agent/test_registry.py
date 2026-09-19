import pytest

from agent.tools import base


@pytest.fixture(autouse=True)
def _clean():
    base.REGISTRY.clear()
    yield
    base.REGISTRY.clear()


def test_dispatch_rejects_unknown_tool():
    out = base.dispatch("nope", {})
    assert out["ok"] is False and "unknown tool" in out["error"]


def test_dispatch_rejects_bad_arguments():
    @base.tool(
        "echo",
        "echo a string",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    def _echo(text: str) -> dict:
        return {"text": text}

    out = base.dispatch("echo", {"wrong": 1})
    assert out["ok"] is False
    assert "text" in out["error"]


def test_dispatch_returns_result_on_valid_call():
    @base.tool(
        "echo",
        "echo a string",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    )
    def _echo(text: str) -> dict:
        return {"text": text}

    assert base.dispatch("echo", {"text": "hi"}) == {"ok": True, "result": {"text": "hi"}}


def test_tool_exception_becomes_typed_error():
    @base.tool("boom", "raises", {"type": "object", "properties": {}})
    def _boom() -> dict:
        raise RuntimeError("kaboom")

    out = base.dispatch("boom", {})
    assert out["ok"] is False and "kaboom" in out["error"]
