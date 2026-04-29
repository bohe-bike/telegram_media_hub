"""Fixed-account web login session API."""

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from app.core.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    WEB_LOGIN_USERNAME,
    check_login_credentials,
    create_session_cookie,
    is_session_request,
)

router = APIRouter(prefix="/session", tags=["session"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    """Authenticate the fixed admin account and set a web session cookie."""
    username = body.username.strip()
    if not check_login_credentials(username, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=create_session_cookie(username),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return {"logged_in": True, "user": {"username": WEB_LOGIN_USERNAME}}


@router.post("/logout")
async def logout(response: Response):
    """Clear the web login session cookie."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", samesite="lax")
    return {"logged_in": False}


@router.get("/me")
async def me(request: Request):
    """Return current web login state."""
    if not is_session_request(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
        )
    return {"logged_in": True, "user": {"username": WEB_LOGIN_USERNAME}}
