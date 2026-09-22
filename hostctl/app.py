import logging
import subprocess
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from hostctl import certs, metrics, mt5, screendump, zfs
from hostctl.auth import require_token
from hostctl.pve import (
    GuestAgentUnavailableError,
    GuestCommandTimeoutError,
    guest_action,
    guest_exec,
    guest_shell,
    host_shell,
    list_guests,
)

logger = logging.getLogger("hostctl.access")

app = FastAPI(title="hostctl")

Endpoint = Callable[[Request], Awaitable[Response]]


@app.middleware("http")
async def log_requests(request: Request, call_next: Endpoint) -> Response:
    """Log every request - method, path, source IP, body, status - to the
    journal, regardless of outcome. hostctl is the privileged boundary of a
    full-autonomy system; without this, the only record of what was asked of
    it is written by the thing an operator would be investigating.

    Deliberately never logs the Authorization header.
    """
    body = await request.body()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        client_host = request.client.host if request.client else "-"
        logger.info(
            "%s %s %s body=%s status=%s",
            client_host,
            request.method,
            request.url.path,
            body.decode("utf-8", errors="replace"),
            status_code,
        )


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


class ActionBody(BaseModel):
    action: Literal["start", "stop", "reboot"]


class ExecBody(BaseModel):
    argv: list[str]


class ShellBody(BaseModel):
    command: str


class SnapshotBody(BaseModel):
    dataset: str
    label: str


@app.get("/guests", dependencies=[Depends(_auth)])
def guests() -> dict[str, object]:
    return {"guests": list_guests()}


@app.post("/guest/{guest_id}/action", dependencies=[Depends(_auth)])
def action(guest_id: int, body: ActionBody) -> dict[str, str]:
    try:
        return guest_action(guest_id, body.action)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _exec_error_to_http(exc: Exception) -> HTTPException:
    """Shared exception -> HTTPException mapping for both exec routes."""
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, GuestAgentUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, GuestCommandTimeoutError):
        return HTTPException(status_code=504, detail=str(exc))
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return HTTPException(status_code=422, detail=detail)
    raise exc  # pragma: no cover - unexpected exception type, let it 500


@app.post("/guest/{guest_id}/exec", dependencies=[Depends(_auth)])
def execute(guest_id: int, body: ExecBody) -> dict[str, object]:
    try:
        return guest_exec(guest_id, body.argv)
    except (
        PermissionError,
        GuestAgentUnavailableError,
        GuestCommandTimeoutError,
        subprocess.CalledProcessError,
    ) as exc:
        raise _exec_error_to_http(exc) from exc


@app.post("/guest/{guest_id}/shell", dependencies=[Depends(_auth)])
def shell(guest_id: int, body: ShellBody) -> dict[str, object]:
    try:
        return guest_shell(guest_id, body.command)
    except (
        PermissionError,
        GuestAgentUnavailableError,
        GuestCommandTimeoutError,
        subprocess.CalledProcessError,
    ) as exc:
        raise _exec_error_to_http(exc) from exc


@app.post("/host/exec", dependencies=[Depends(_auth)])
def host_exec(body: ShellBody) -> dict[str, object]:
    """Run a free-form shell command directly on the Proxmox host - the
    counterpart to /guest/{id}/shell, but for the machine hostctl itself
    runs on. See hostctl.pve.host_shell for what that means in practice."""
    try:
        return host_shell(body.command)
    except (PermissionError, GuestCommandTimeoutError, subprocess.CalledProcessError) as exc:
        raise _exec_error_to_http(exc) from exc


@app.get("/zfs/status", dependencies=[Depends(_auth)])
def zfs_status() -> dict[str, object]:
    return zfs.status()


@app.post("/zfs/snapshot", dependencies=[Depends(_auth)])
def zfs_snapshot(body: SnapshotBody) -> dict[str, str]:
    try:
        return {"snapshot": zfs.snapshot(body.dataset, body.label)}
    except zfs.SnapshotRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except zfs.SnapshotCapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/zfs/scrub", dependencies=[Depends(_auth)])
def zfs_scrub() -> dict[str, str]:
    return zfs.scrub()


@app.get("/host/metrics", dependencies=[Depends(_auth)])
def host_metrics() -> dict[str, float]:
    return metrics.host()


@app.get("/mt5/status", dependencies=[Depends(_auth)])
def mt5_status() -> dict[str, object]:
    return mt5.status()


@app.get("/certs/status", dependencies=[Depends(_auth)])
def certs_status() -> dict[str, object]:
    return certs.status()


@app.post("/mt5/screenshot", dependencies=[Depends(_auth)])
def mt5_screenshot() -> Response:
    try:
        png = screendump.capture_png()
    except screendump.ScreendumpTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except screendump.ScreendumpConversionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=png, media_type="image/png")
