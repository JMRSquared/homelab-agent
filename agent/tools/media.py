import os
from typing import Any
from urllib.parse import urlencode

import httpx

from agent.clients import TIMEOUT, service_get
from agent.tools.base import tool

JELLYFIN = "http://10.0.0.165:8096"
JELLYSEERR = "http://10.0.0.165:5055"
KINDS = ("movie", "show")

QUERY_SCHEMA = {"type": "string", "minLength": 1}


def _jf_headers() -> dict[str, str]:
    return {"Authorization": f'MediaBrowser Token="{os.environ["JELLYFIN_KEY"]}"'}


def _js_headers() -> dict[str, str]:
    return {"X-Api-Key": os.environ["JELLYSEERR_KEY"]}


def _require_query(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("query must not be blank")
    return query


@tool(
    "media_search",
    "Check whether a film or show is ALREADY in the Jellyfin library (read-only lookup). "
    "Use this first to answer 'do we have X' or 'is X on Jellyfin'. Does not request or "
    "download anything — call media_request for that once you know it's missing.",
    {
        "type": "object",
        "properties": {"query": {**QUERY_SCHEMA}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
def media_search(query: str) -> dict[str, Any]:
    query = _require_query(query)
    r = httpx.get(
        f"{JELLYFIN}/Items",
        params={"searchTerm": query, "Recursive": "true", "Limit": 10},
        headers=_jf_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


@tool(
    "media_request",
    "Request a film or show that is NOT already available, so Jellyseerr fetches it and "
    "it later appears in Jellyfin. Only call this after media_search (or a family member "
    "saying it's missing) confirms it isn't already on Jellyfin — this does not check the "
    "existing library itself, and calling it for something already available is wasted work.",
    {
        "type": "object",
        "properties": {
            "query": {**QUERY_SCHEMA},
            "kind": {"type": "string", "enum": list(KINDS)},
        },
        "required": ["query", "kind"],
        "additionalProperties": False,
    },
)
def media_request(query: str, kind: str) -> dict[str, Any]:
    query = _require_query(query)
    if kind not in KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    search_path = f"/api/v1/search?{urlencode({'query': query})}"
    found = service_get(JELLYSEERR, search_path, _js_headers())
    results = [r for r in found.get("results", []) if r.get("mediaType") == kind]
    if not results:
        return {"requested": False, "reason": f"no {kind} matched {query!r}"}
    r = httpx.post(
        f"{JELLYSEERR}/api/v1/request",
        json={"mediaId": results[0]["id"], "mediaType": kind},
        headers=_js_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {"requested": True, "jellyseerr": r.json()}


# Jellyfin's runtime is stored in 100ns ticks. 600_000_000 ticks/minute
# (10_000_000 ticks/sec * 60).
_TICKS_PER_MINUTE = 600_000_000


@tool(
    "media_last_watched",
    "Report the most recently watched movie(s) on Jellyfin - title, year, runtime in "
    "minutes, and when it was watched. Checks every Jellyfin user's history and merges "
    "them, since watch history can sit under any account, not necessarily the obvious "
    "one. Use this to answer 'what did we last watch' or 'how long was the last movie'. "
    "`count` (default 1) returns that many most-recent items instead of just the latest.",
    {
        "type": "object",
        "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 25}},
        "required": [],
        "additionalProperties": False,
    },
)
def media_last_watched(count: int = 1) -> dict[str, Any]:
    if count < 1:
        raise ValueError("count must be at least 1")

    users_resp = httpx.get(f"{JELLYFIN}/Users", headers=_jf_headers(), timeout=TIMEOUT)
    users_resp.raise_for_status()
    users = users_resp.json()

    watched: list[dict[str, Any]] = []
    for user in users:
        uid = user.get("Id")
        if not uid:
            continue
        r = httpx.get(
            f"{JELLYFIN}/Users/{uid}/Items",
            params={
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "Filters": "IsPlayed",
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Limit": count,
                "Fields": "RunTimeTicks,UserData,ProductionYear",
            },
            headers=_jf_headers(),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        for item in r.json().get("Items", []):
            played_at = (item.get("UserData") or {}).get("LastPlayedDate")
            if not played_at:
                continue
            ticks = item.get("RunTimeTicks")
            watched.append(
                {
                    "title": item.get("Name"),
                    "year": item.get("ProductionYear"),
                    "runtime_minutes": round(ticks / _TICKS_PER_MINUTE) if ticks else None,
                    "watched_at": played_at,
                    "watched_by": user.get("Name"),
                }
            )

    if not watched:
        return {
            "items": [],
            "note": "no watch history found for any Jellyfin user",
        }

    watched.sort(key=lambda w: w["watched_at"], reverse=True)
    return {"items": watched[:count]}


@tool(
    "media_library_status",
    "Jellyfin item counts and pending Jellyseerr requests. Use this for library-wide "
    "totals or to see what's still pending, not to check a single title.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def media_library_status() -> dict[str, Any]:
    counts = service_get(JELLYFIN, "/Items/Counts", _jf_headers())
    pending = service_get(JELLYSEERR, "/api/v1/request?filter=pending", _js_headers())
    return {"jellyfin": counts, "pending_requests": pending}
