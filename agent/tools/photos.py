import os
from typing import Any

import httpx

from agent.clients import TIMEOUT, service_get
from agent.tools.base import tool

IMMICH = "http://10.0.0.165:2283"


def _headers() -> dict[str, str]:
    return {"x-api-key": os.environ["IMMICH_KEY"]}


@tool(
    "photos_search",
    "Search the family photo library by describing what's in the picture (subject, "
    "place, people). Read-only smart search against Immich; never modifies Immich "
    "configuration or data.",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def photos_search(query: str) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise ValueError("query must not be blank")
    r = httpx.post(
        f"{IMMICH}/api/search/smart",
        json={"query": query, "size": 10},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


@tool(
    "photos_stats",
    "Photo and video counts and storage used by Immich.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def photos_stats() -> dict[str, Any]:
    return service_get(IMMICH, "/api/server/statistics", _headers())
