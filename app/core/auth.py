"""API key authentication dependency.

If `api_secret_key` is empty in settings, authentication is disabled (first-time
setup mode). Once a key is configured, all protected API routes require the header:

    X-API-Key: <your-secret-key>
"""

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.settings import settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Verify the API key from the X-API-Key header.

    Skips verification when api_secret_key is not configured (first-time setup).
    """
    secret = settings.api_secret_key
    if not secret:
        # Not yet configured – allow access for initial setup
        return

    if not api_key or api_key != secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key. Set X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
