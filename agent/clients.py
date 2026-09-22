import os
from typing import Any

import httpx
from openai import OpenAI

TIMEOUT = httpx.Timeout(30.0)

# For exec-backed tools whose underlying command can genuinely take minutes
# (e.g. `docker compose pull` for a large image) rather than the sub-second
# calls everything else in this module makes. Must stay >= hostctl's own
# `pve.EXEC_TIMEOUT` (the subprocess timeout `guest_exec` runs under on the
# host) plus headroom for the HTTP round trip itself, or the agent would give
# up on a call hostctl was still legitimately running.
EXEC_TIMEOUT = httpx.Timeout(330.0)


def _hostctl_base() -> str:
    return os.environ.get("HOSTCTL_URL", "http://10.0.0.2:8710")


def _hostctl_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['HOSTCTL_TOKEN']}"}


def hostctl_get(path: str, *, timeout: httpx.Timeout = TIMEOUT) -> dict[str, Any]:
    r = httpx.get(f"{_hostctl_base()}{path}", headers=_hostctl_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def hostctl_post(
    path: str, body: dict[str, Any], *, timeout: httpx.Timeout = TIMEOUT
) -> dict[str, Any]:
    r = httpx.post(
        f"{_hostctl_base()}{path}", json=body, headers=_hostctl_headers(), timeout=timeout
    )
    _raise_with_detail_on_422(r)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _raise_with_detail_on_422(r: httpx.Response) -> None:
    """Re-raise on 422 with the hostctl `detail` field in the message.

    hostctl returns 422 when the underlying subprocess (the shell command
    the agent asked the guest or host to run) returned a non-zero exit
    code - it is NOT a malformed request. The command's stderr is placed
    into the response body's `detail` field (see hostctl's
    `_exec_error_to_http`). The default httpx message - "Client error
    '422 Unprocessable Content' for url ..." - hides that detail, so the
    model sees a wall of HTTP jargon and no signal about why the command
    failed (e.g. `grep: ...: No such file or directory` vs a bare
    exit-1 from `grep -c` finding zero matches). Surfacing `detail` is
    what lets the caller tell those cases apart instead of retrying.
    """
    if r.status_code != 422:
        return
    detail = ""
    try:
        body = r.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        raw = body.get("detail")
        if isinstance(raw, str):
            detail = raw
        elif raw is not None:
            detail = str(raw)
    msg = f"hostctl 422: {detail}" if detail else "hostctl 422 (no detail)"
    raise httpx.HTTPStatusError(msg, request=r.request, response=r)


def service_get(base: str, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = httpx.get(f"{base}{path}", headers=headers or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def hostctl_post_bytes(
    path: str, *, timeout: httpx.Timeout = EXEC_TIMEOUT
) -> tuple[bytes, str]:
    """Like `hostctl_post`, but for a route that returns a raw binary body
    (e.g. a screenshot) instead of JSON. Returns (body, content-type)."""
    r = httpx.post(f"{_hostctl_base()}{path}", headers=_hostctl_headers(), timeout=timeout)
    _raise_with_detail_on_422(r)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "application/octet-stream")


def minimax_model() -> str:
    return os.environ.get("MINIMAX_MODEL", "MiniMax-M3")


def minimax_client() -> OpenAI:
    """A plain, synchronous MiniMax client built from the same env vars
    `agent/config.py` uses for the agent's own model client, but reading
    them directly here rather than going through `config.load()` - that
    loads Settings, which requires the Slack/hostctl tokens too, and a tool
    that only needs a model client shouldn't fail to construct one over an
    unrelated missing var.

    Deliberately not shared with `agent/model.py`'s `Agent` class (which
    builds its own `AsyncOpenAI` in `__init__`): `Agent` is async and holds
    conversation/audit state a tool has no business touching, and importing
    it here to reach its client would risk a tool re-entering the model
    loop. This is the "construct a plain client from the same env vars"
    option, not the "share Agent's instance" option - see the image_inspect
    module docstring for how it's used.
    """
    return OpenAI(
        api_key=os.environ["MINIMAX_API_KEY"],
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1"),
    )


def hostctl_get_optional(path: str, *, timeout: httpx.Timeout = TIMEOUT) -> dict[str, Any] | None:
    """`hostctl_get`, but returns None instead of raising when the route
    simply isn't there.

    For collectors that watch something hostctl may not expose yet (the
    cert-expiry route is being built separately). A 404/405 means "this
    hostctl build predates the route", and a transport error means hostctl
    itself is unreachable - which the rest of the tick already reports
    loudly through its own collectors. Neither is a reason for the
    optional collector to manufacture an error of its own, so both
    degrade to None. A 500 is a real server-side fault and still raises.
    """
    try:
        r = httpx.get(f"{_hostctl_base()}{path}", headers=_hostctl_headers(), timeout=timeout)
    except httpx.RequestError:
        return None
    if r.status_code in (404, 405, 501):
        return None
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]
