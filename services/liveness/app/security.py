import hmac

from fastapi import Depends, Header, HTTPException, Request

from .config import Settings


def require_api_key(request: Request, x_api_key: str = Header("", alias="X-API-Key")) -> None:
    """Every /v1 route needs one of the configured keys (constant-time comparison)."""
    s: Settings = request.app.state.settings
    if s.disable_auth:
        return
    if not x_api_key or not any(hmac.compare_digest(x_api_key, k) for k in s.keys):
        raise HTTPException(401, "Missing or invalid API key (send it in the X-API-Key header).")
