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
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


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
