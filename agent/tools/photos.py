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


def _search_items(query: str) -> list[dict[str, Any]]:
    """Immich's smart-search response nests hits under assets.items. Raise a
    clear, typed error when nothing matched rather than letting an IndexError
    or KeyError reach the model as a cryptic traceback - "no hits for this
    query" is a normal, expected outcome the model needs to be able to relay,
    not a bug."""
    r = httpx.post(
        f"{IMMICH}/api/search/smart",
        json={"query": query, "size": 10},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    items: list[dict[str, Any]] = r.json().get("assets", {}).get("items", [])
    if not items:
        raise ValueError(f"no photos matched {query!r}")
    return items


def _candidate_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"index": i, "asset_id": item.get("id"), "filename": item.get("originalFileName")}
        for i, item in enumerate(items)
    ]


# Immich ranks by embedding similarity, not exact identification - a query
# for one specific variant of something (a model of car, a particular pet)
# can and does rank a visually near-identical but wrong result first (see
# the capability brief's own example: "black audi rs3" ranked an S3 above
# the actual RS3, which was third). This note rides along on every result so
# the model can't reasonably claim it wasn't told the match is unverified.
_CONFIDENCE_NOTE = (
    "This is a visual-similarity search match, not a verified one - the top result can be "
    "a near-identical but wrong variant of what was asked for. If the exact subject matters "
    "(a specific model, a specific pet, anything where being wrong would be embarrassing), "
    "call image_inspect on the downloaded path to confirm before attaching or reporting it as "
    "correct. If it's wrong, call photos_download again with a different `index`, or with "
    "`asset_id` set to another entry from `candidates`."
)


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
    "Smart-search the family photo library and download a matching image to the local "
    "outbox, ready to attach to an email or message - OR, given asset_id, download that "
    "exact asset directly (e.g. one seen in a previous call's candidates list). By default "
    "downloads the JPEG preview rendition (Immich's resized, always-mailable copy), NOT the "
    "full original - originals from this library can be many-megabyte TIFFs that most mail "
    "servers reject as attachments. Pass original=true only when the actual source file is "
    "specifically needed (e.g. for archival), not for a routine 'send a photo of X' request. "
    "IMPORTANT: Immich ranks by visual similarity, not correctness - the top match for a "
    "query naming something specific (a car model, a pet) can be a near-identical but wrong "
    "result. The result's `candidates` list and `note` explain this every time; call "
    "image_inspect on the downloaded path to verify before treating a specific-subject match "
    "as correct, and re-call this with a different `index` or `asset_id` if it's wrong.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "asset_id": {"type": "string"},
            "index": {"type": "integer", "minimum": 0, "maximum": 9},
            "original": {"type": "boolean"},
        },
        "required": [],
        "additionalProperties": False,
    },
)
def photos_download(
    query: str | None = None,
    asset_id: str | None = None,
    index: int = 0,
    original: bool = False,
) -> dict[str, Any]:
    query = (query or "").strip() or None
    asset_id = (asset_id or "").strip() or None
    if not query and not asset_id:
        raise ValueError("provide either query or asset_id")
    if query and asset_id:
        raise ValueError("provide only one of query or asset_id, not both")
    if index < 0:
        raise ValueError("index must not be negative")

    candidates: list[dict[str, Any]] = []
    if query:
        items = _search_items(query)
        candidates = _candidate_summary(items)
        if index >= len(items):
            raise ValueError(
                f"index {index} out of range - only {len(items)} candidates matched {query!r}"
            )
        asset = items[index]
        chosen_id = asset["id"]
        source_name = asset.get("originalFileName", chosen_id)
    else:
        assert asset_id is not None  # narrowed by the check above
        chosen_id = asset_id
        meta = httpx.get(
            f"{IMMICH}/api/assets/{chosen_id}", headers=_headers(), timeout=TIMEOUT
        )
        meta.raise_for_status()
        source_name = meta.json().get("originalFileName", chosen_id)

    rendition = "original" if original else "thumbnail"
    params = {} if original else {"size": "preview"}
    dl = httpx.get(
        f"{IMMICH}/api/assets/{chosen_id}/{rendition}",
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
        "asset_id": chosen_id,
        "original_filename": source_name,
        "rendition": "original" if original else "preview_jpeg",
        "content_type": content_type,
        "byte_size": len(dl.content),
        "width": dims[0] if dims else None,
        "height": dims[1] if dims else None,
        "candidates": candidates,
        "note": _CONFIDENCE_NOTE,
    }


@tool(
    "photos_stats",
    "Photo and video counts and storage used by Immich.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
def photos_stats() -> dict[str, Any]:
    return service_get(IMMICH, "/api/server/statistics", _headers())
