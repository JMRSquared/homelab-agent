from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jsonschema


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    fn: Callable[..., dict[str, Any]]


REGISTRY: dict[str, Tool] = {}


def tool(
    name: str, description: str, schema: dict[str, Any]
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def wrap(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        REGISTRY[name] = Tool(name=name, description=description, schema=schema, fn=fn)
        return fn

    return wrap


def openai_schema() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema},
        }
        for t in REGISTRY.values()
    ]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = REGISTRY.get(name)
    if entry is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    try:
        jsonschema.validate(args, entry.schema)
    except jsonschema.ValidationError as exc:
        return {"ok": False, "error": f"invalid arguments: {exc.message}"}
    try:
        return {"ok": True, "result": entry.fn(**args)}
    except Exception as exc:  # surfaced to the model as a typed error, never raised through
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
