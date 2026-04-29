"""Web login and API key authentication helpers.

Protected API routes accept either:

- a valid web-login session cookie, or
- the header X-API-Key: <your-secret-key> when api_secret_key is configured.
"""

import hmac
import time
from hashlib import sha256

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.core.settings import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

WEB_LOGIN_USERNAME = "admin"
WEB_LOGIN_PASSWORD = "songbike.7799"
SESSION_COOKIE_NAME = "tmh_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600


def _signing_secret() -> bytes:
    secret = settings.api_secret_key or f"{WEB_LOGIN_USERNAME}:{WEB_LOGIN_PASSWORD}:telegram-media-hub"
    return secret.encode("utf-8")


def _session_signature(payload: str) -> str:
    return hmac.new(_signing_secret(), payload.encode("utf-8"), sha256).hexdigest()


def create_session_cookie(username: str = WEB_LOGIN_USERNAME) -> str:
    expires_at = int(time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"{username}:{expires_at}"
    return f"{payload}:{_session_signature(payload)}"


def is_valid_session_token(token: str | None) -> bool:
    if not token:
        return False

    try:
        username, expires_at_raw, signature = token.rsplit(":", 2)
        expires_at = int(expires_at_raw)
    except (TypeError, ValueError):
        return False

    if username != WEB_LOGIN_USERNAME or expires_at < int(time.time()):
        return False

    payload = f"{username}:{expires_at}"
    return hmac.compare_digest(signature, _session_signature(payload))


def is_session_request(request: Request) -> bool:
    return is_valid_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def check_login_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, WEB_LOGIN_USERNAME) and hmac.compare_digest(
        password, WEB_LOGIN_PASSWORD
    )


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(_api_key_header),
) -> None:
    """Verify either the web-login cookie or configured API key."""
    if is_session_request(request):
        return

    secret = settings.api_secret_key
    if secret and api_key and hmac.compare_digest(api_key, secret):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Login required.",
        headers={"WWW-Authenticate": "ApiKey"},
    )
