import hmac

from fastapi import Depends, Header, HTTPException, Query

from app.config import Settings, get_settings


def _matches(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def require_cron(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    prefix = "Bearer "
    token = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else None
    if not _matches(token, settings.cron_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_admin(
    x_admin_secret: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not _matches(x_admin_secret, settings.admin_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")


def require_webhook(
    token: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not _matches(token, settings.apify_webhook_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")
