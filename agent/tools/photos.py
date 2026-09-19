import os
from typing import Any

import httpx

from agent.clients import TIMEOUT, service_get
from agent.tools import imaging, outbox
from agent.tools.base import tool

IMMICH = "http://10.0.0.165:2283"

# Immich's own timeout is fine for the search call, but a full-size original
# can be tens of megabytes over a LAN link that's still just a link - give
# the download itself more room than the default 30s.
DOWNLOAD_TIMEOUT = httpx.Timeout(120.0)


def _headers() -> dict[str, str]:
    return {"x-api-key": os.environ["IMMICH_KEY"]}


def _best_match(search_result: dict[str, Any]) -> dict[str, Any]:
    """Immich's smart-search response nests hits under assets.items. Raise a
    clear, typed error when nothing matched rather than letting an IndexError
    or KeyError reach the model as a cryptic traceback - "no hits for this
    query" is a normal, expected outcome the model needs to be able to relay,
    not a bug."""
    items = search_result.get("assets", {}).get("items", [])
    if not items:
        raise ValueError("no photos matched that search")
    return items[0]  # type: ignore[no-any-return]


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
    "photos_download",
    "Smart-search the family photo library and download the best-matching image to the "
    "local outbox, ready to attach to an email or message. By default downloads the JPEG "
    "preview rendition (Immich's resized, always-mailable copy), NOT the full original - "
    "originals from this library can be many-megabyte TIFFs that most mail servers reject "
    "as attachments. Pass original=true only when the actual source file is specifically "
    "needed (e.g. for archival), not for a routine 'send a photo of X' request. Returns the "
    "local file path, pixel dimensions (when they could be read), byte size, and the "
    "original filename from Immich.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1},
            "original": {"type": "boolean"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def photos_download(query: str, original: bool = False) -> dict[str, Any]:
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
    asset = _best_match(r.json())
    asset_id = asset["id"]
    source_name = asset.get("originalFileName", asset_id)

    path = "original" if original else "thumbnail"
    params = {} if original else {"size": "preview"}
    dl = httpx.get(
        f"{IMMICH}/api/assets/{asset_id}/{path}",
        params=params,
        headers=_headers(),
        timeout=DOWNLOAD_TIMEOUT,
    )
    dl.raise_for_status()
    content_type = dl.headers.get("content-type", "application/octet-stream")

    suffix = os.path.splitext(source_name)[1] if original else ".jpg"
    if not suffix:
        suffix = ".bin"
    stem = os.path.splitext(source_name)[0] or "photo"
    dest = outbox.new_artifact_path(stem, suffix)
    dest.write_bytes(dl.content)

    dims = imaging.dimensions(dl.content, content_type)
    return {
        "path": str(dest),
        "original_filename": source_name,
        "rendition": "original" if original else "preview_jpeg",
        "content_type": content_type,
        "byte_size": len(dl.content),
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
    }


@tool(
    "photos_stats",
    "Photo and video counts and storage used by Immich.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def photos_stats() -> dict[str, Any]:
    return service_get(IMMICH, "/api/server/statistics", _headers())
