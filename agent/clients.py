import os
from typing import Any

import httpx

TIMEOUT = httpx.Timeout(30.0)


def _hostctl_base() -> str:
    return os.environ.get("HOSTCTL_URL", "http://10.0.0.2:8710")


def _hostctl_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['HOSTCTL_TOKEN']}"}


def hostctl_get(path: str) -> dict[str, Any]:
    r = httpx.get(f"{_hostctl_base()}{path}", headers=_hostctl_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def hostctl_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{_hostctl_base()}{path}", json=body, headers=_hostctl_headers(), timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def service_get(base: str, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = httpx.get(f"{base}{path}", headers=headers or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]
