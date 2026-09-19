from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from hostctl import metrics, zfs
from hostctl.auth import require_token
from hostctl.pve import BLOCKED_GUEST_IDS, guest_action, guest_exec, list_guests

app = FastAPI(title="hostctl")


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


class ActionBody(BaseModel):
    action: Literal["start", "stop", "reboot"]


class ExecBody(BaseModel):
    argv: list[str]


class SnapshotBody(BaseModel):
    dataset: str
    label: str


def _guard(guest_id: int) -> None:
    if guest_id in BLOCKED_GUEST_IDS:
        raise HTTPException(status_code=403, detail="guest is out of scope")


@app.get("/guests", dependencies=[Depends(_auth)])
def guests() -> dict[str, object]:
    return {"guests": list_guests()}


@app.post("/guest/{guest_id}/action", dependencies=[Depends(_auth)])
def action(guest_id: int, body: ActionBody) -> dict[str, str]:
    _guard(guest_id)
    return guest_action(guest_id, body.action)


@app.post("/guest/{guest_id}/exec", dependencies=[Depends(_auth)])
def execute(guest_id: int, body: ExecBody) -> dict[str, object]:
    _guard(guest_id)
    try:
        return guest_exec(guest_id, body.argv)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/zfs/status", dependencies=[Depends(_auth)])
def zfs_status() -> dict[str, object]:
    return zfs.status()


@app.post("/zfs/snapshot", dependencies=[Depends(_auth)])
def zfs_snapshot(body: SnapshotBody) -> dict[str, str]:
    try:
        return {"snapshot": zfs.snapshot(body.dataset, body.label)}
    except zfs.SnapshotRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/zfs/scrub", dependencies=[Depends(_auth)])
def zfs_scrub() -> dict[str, str]:
    return zfs.scrub()


@app.get("/host/metrics", dependencies=[Depends(_auth)])
def host_metrics() -> dict[str, float]:
    return metrics.host()
