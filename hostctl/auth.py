import hmac
import os

from fastapi import HTTPException


def require_token(authorization: str | None) -> None:
    expected = os.environ.get("HOSTCTL_TOKEN", "")
    prefix = "Bearer "
    if not expected or not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="unauthorized")
