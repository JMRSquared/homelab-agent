import os
from typing import Any

import httpx

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
