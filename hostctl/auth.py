import hmac
import os

from fastapi import HTTPException


def _expected_token() -> str:
    token_file = os.environ.get("HOSTCTL_TOKEN_FILE", "")
    if token_file:
        try:
            with open(token_file) as f:
                return f.read().strip()
        except OSError:
            return ""
    return os.environ.get("HOSTCTL_TOKEN", "")


def require_token(authorization: str | None) -> None:
    expected = _expected_token()
    prefix = "Bearer "
    if not expected or not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=401, detail="unauthorized")
