from fastapi import Depends, FastAPI, Header

from hostctl.auth import require_token
from hostctl.pve import list_guests

app = FastAPI(title="hostctl")


def _auth(authorization: str | None = Header(default=None)) -> None:
    require_token(authorization)


@app.get("/guests", dependencies=[Depends(_auth)])
def guests() -> dict[str, object]:
    return {"guests": list_guests()}
