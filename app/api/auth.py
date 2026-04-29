"""Telegram authentication API – web-based interactive login flow.

Flow:
  1. POST /api/auth/send-code   {phone}           -> sends SMS / TG code
  2. POST /api/auth/sign-in     {phone, code}      -> verifies the code
  3. POST /api/auth/sign-in-2fa {password}          -> 2FA (if required)
  4. GET  /api/auth/status                          -> check session state
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel
from pyrogram import Client
from pyrogram.errors import (
    BadRequest,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
)

from app.core.tg_client import export_session_to_redis
from app.core.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_auth_key_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "auth key" in msg or "transport error: 404" in msg or "auth_key_duplicated" in msg

# ---------- in-memory auth state (single-user, single-login at a time) ---


class _AuthState:
    client: Optional[Client] = None
    phone: str = ""
    phone_code_hash: str = ""
    needs_2fa: bool = False
    logged_in: bool = False


_state = _AuthState()


# ---------- schemas -------------------------------------------------------

class SendCodeReq(BaseModel):
    phone: str


class SignInReq(BaseModel):
    phone: str
    code: str


class TwoFAReq(BaseModel):
    password: str


# ---------- helpers -------------------------------------------------------

def _make_auth_client() -> Client:
    return Client(
        name=f"{settings.tg_session_name}_auth",
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        phone_number=None,          # we handle auth manually
        workdir=str(settings.session_dir),
        in_memory=False,            # persist to file
        proxy=settings.tg_proxy,
    )


def _make_main_session_client() -> Client:
    return Client(
        name=settings.tg_session_name,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        phone_number=None,
        workdir=str(settings.session_dir),
        in_memory=False,
        proxy=settings.tg_proxy,
    )


def _session_file_exists() -> bool:
    p = settings.session_dir / f"{settings.tg_session_name}.session"
    return p.exists()


def _main_session_file():
    return settings.session_dir / f"{settings.tg_session_name}.session"


def _auth_session_file():
    return settings.session_dir / f"{settings.tg_session_name}_auth.session"


def _clear_session_files() -> None:
    _main_session_file().unlink(missing_ok=True)
    _auth_session_file().unlink(missing_ok=True)


async def _promote_auth_session() -> None:
    """Replace main session with the successful auth-flow session atomically."""
    auth_file = _auth_session_file()
    if not auth_file.exists():
        raise HTTPException(500, "Login succeeded but auth session file was not found")
    _main_session_file().unlink(missing_ok=True)
    auth_file.replace(_main_session_file())


# ---------- routes --------------------------------------------------------

@router.get("/status")
async def auth_status():
    """Check whether a valid session already exists."""
    from app.services.telegram import tg_listener

    has_session = _session_file_exists()

    # If the main listener is already running, read user info directly from it
    # instead of opening a second Client (which would lock the SQLite session).
    if tg_listener.is_running:
        try:
            me = await tg_listener.client.get_me()
            _state.logged_in = True
            return {
                "logged_in": True,
                "has_session": True,
                "user": {
                    "id": me.id,
                    "first_name": me.first_name,
                    "last_name": me.last_name or "",
                    "username": me.username or "",
                    "phone": me.phone_number or "",
                },
            }
        except Exception as e:
            logger.debug(f"get_me from listener failed: {e}")
            if _is_auth_key_error(e):
                logger.warning("Listener session became invalid (AUTH_KEY_DUPLICATED), clearing session.")
                try:
                    await tg_listener.stop()
                except Exception:
                    pass
                _clear_session_files()
                try:
                    from app.core.redis import redis_conn as _rc
                    _rc.delete("tg:session_string")
                    _rc.delete("tg:session_gen")
                except Exception:
                    pass
                _state.logged_in = False
                return {
                    "logged_in": False,
                    "has_session": False,
                    "needs_2fa": False,
                    "user": None,
                }

    # Listener not running – try a one-shot probe with a fresh client.
    # Use a short timeout so a dead/incomplete session never hangs the UI.
    if has_session and not _state.logged_in:
        try:
            c = _make_main_session_client()
            await asyncio.wait_for(c.start(), timeout=15)
            me = await c.get_me()
            await c.stop()
            _state.logged_in = True
            return {
                "logged_in": True,
                "has_session": True,
                "user": {
                    "id": me.id,
                    "first_name": me.first_name,
                    "last_name": me.last_name or "",
                    "username": me.username or "",
                    "phone": me.phone_number or "",
                },
            }
        except asyncio.TimeoutError:
            logger.warning("Session probe timed out – session may be invalid.")
            _state.logged_in = False
            # Remove the stale session file so Pyrogram won't prompt again
            _main_session_file().unlink(missing_ok=True)
            has_session = False
        except Exception as e:
            _state.logged_in = False
            if _is_auth_key_error(e):
                logger.warning(f"Session probe failed (auth key invalid): {e}")
                _main_session_file().unlink(missing_ok=True)
                has_session = False
                try:
                    from app.core.redis import redis_conn as _rc
                    _rc.delete("tg:session_string")
                    _rc.delete("tg:session_gen")
                except Exception:
                    pass
            else:
                logger.debug(f"Session probe failed: {e}")

    return {
        "logged_in": _state.logged_in,
        "has_session": has_session,
        "needs_2fa": _state.needs_2fa,
        "user": None,
    }


@router.post("/send-code")
async def send_code(body: SendCodeReq):
    """Step 1: send verification code to phone number."""
    if not settings.tg_api_id or not settings.tg_api_hash:
        raise HTTPException(
            400, "TG_API_ID and TG_API_HASH must be configured first (Settings page)")

    phone = body.phone.strip()
    if not phone:
        raise HTTPException(400, "Phone number is required")

    # Tear down any previous half-open client
    if _state.client:
        try:
            await _state.client.disconnect()
        except Exception:
            pass

    # Always start a fresh auth-flow session file for this login attempt.
    _auth_session_file().unlink(missing_ok=True)

    client = _make_auth_client()
    try:
        await client.connect()
    except sqlite3.OperationalError as e:
        raise HTTPException(
            409,
            "Telegram session database is locked. Please retry in a few seconds.",
        ) from e

    try:
        sent = await client.send_code(phone)
    except BadRequest as e:
        await client.disconnect()
        raise HTTPException(400, f"Telegram rejected the request: {e.MESSAGE}")
    except Exception as e:
        await client.disconnect()
        raise HTTPException(500, f"Failed to send code: {e}")

    _state.client = client
    _state.phone = phone
    _state.phone_code_hash = sent.phone_code_hash
    _state.needs_2fa = False
    _state.logged_in = False

    logger.info(f"Verification code sent to {phone}")
    return {"message": f"Verification code sent to {phone}", "phone": phone}


@router.post("/sign-in")
async def sign_in(body: SignInReq):
    """Step 2: verify the code received via SMS / Telegram."""
    if not _state.client or not _state.phone_code_hash:
        raise HTTPException(400, "No pending login. Call /send-code first.")

    code = body.code.strip().replace(" ", "").replace("-", "")
    if not code:
        raise HTTPException(400, "Code is required")

    try:
        await _state.client.sign_in(
            phone_number=_state.phone,
            phone_code_hash=_state.phone_code_hash,
            phone_code=code,
        )
    except SessionPasswordNeeded:
        _state.needs_2fa = True
        return {"message": "2FA password required", "needs_2fa": True}
    except PhoneCodeInvalid:
        raise HTTPException(400, "Invalid verification code")
    except PhoneCodeExpired:
        raise HTTPException(400, "Verification code expired, please resend")
    except Exception as e:
        raise HTTPException(500, f"Sign-in failed: {e}")

    # Success
    me = await _state.client.get_me()
    await _state.client.disconnect()

    await _promote_auth_session()

    # Refresh listener with the newly logged-in session so auth/status and
    # incoming task listening recover immediately.
    from app.services.telegram import tg_listener
    listener_started_ok = False
    try:
        if tg_listener.is_running:
            await tg_listener.stop()
        await asyncio.wait_for(tg_listener.start(), timeout=60)
        listener_started_ok = True
    except Exception as exc:
        logger.warning(f"Listener restart after login failed: {exc}")

    # Ensure the session string is in Redis even if listener restart failed.
    # Use a temporary client to export if needed.
    if not listener_started_ok:
        try:
            c = _make_main_session_client()
            await asyncio.wait_for(c.start(), timeout=15)
            await export_session_to_redis(c)
            await c.stop()
            logger.info("Session string exported to Redis via fallback client after login.")
        except Exception as exc:
            logger.warning(f"Fallback session export after login failed: {exc}")

    _state.logged_in = True
    _state.client = None
    _state.phone_code_hash = ""

    logger.info(f"Logged in as {me.first_name} (ID: {me.id})")
    return {
        "message": "Login successful",
        "needs_2fa": False,
        "user": {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name or "",
            "username": me.username or "",
            "phone": me.phone_number or "",
        },
    }


@router.post("/sign-in-2fa")
async def sign_in_2fa(body: TwoFAReq):
    """Step 3 (optional): provide 2FA cloud password."""
    if not _state.client:
        raise HTTPException(400, "No pending login session")
    if not _state.needs_2fa:
        raise HTTPException(400, "2FA is not required for this session")

    password = body.password
    if not password:
        raise HTTPException(400, "Password is required")

    try:
        await _state.client.check_password(password)
    except BadRequest as e:
        raise HTTPException(400, f"Wrong password: {e.MESSAGE}")
    except Exception as e:
        raise HTTPException(500, f"2FA failed: {e}")

    me = await _state.client.get_me()
    await _state.client.disconnect()

    await _promote_auth_session()

    from app.services.telegram import tg_listener
    listener_started_ok = False
    try:
        if tg_listener.is_running:
            await tg_listener.stop()
        await asyncio.wait_for(tg_listener.start(), timeout=60)
        listener_started_ok = True
    except Exception as exc:
        logger.warning(f"Listener restart after 2FA login failed: {exc}")

    # Ensure the session string is in Redis even if listener restart failed.
    if not listener_started_ok:
        try:
            c = _make_main_session_client()
            await asyncio.wait_for(c.start(), timeout=15)
            await export_session_to_redis(c)
            await c.stop()
            logger.info("Session string exported to Redis via fallback client after 2FA login.")
        except Exception as exc:
            logger.warning(f"Fallback session export after 2FA login failed: {exc}")

    _state.logged_in = True
    _state.needs_2fa = False
    _state.client = None
    _state.phone_code_hash = ""

    logger.info(f"Logged in (2FA) as {me.first_name} (ID: {me.id})")
    return {
        "message": "Login successful (2FA)",
        "user": {
            "id": me.id,
            "first_name": me.first_name,
            "last_name": me.last_name or "",
            "username": me.username or "",
            "phone": me.phone_number or "",
        },
    }


@router.post("/logout")
async def logout():
    """Disconnect and remove session file."""
    if _state.client:
        try:
            await _state.client.disconnect()
        except Exception:
            pass
        _state.client = None

    _clear_session_files()
    logger.info("Session files removed.")

    try:
        from app.core.redis import redis_conn as _rc
        _rc.delete("tg:session_string")
        _rc.delete("tg:session_gen")
    except Exception:
        pass

    _state.logged_in = False
    _state.needs_2fa = False
    _state.phone_code_hash = ""

    return {"message": "Logged out, session removed"}
