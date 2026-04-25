"""Telegram authentication API – web-based interactive login flow.

Flow:
  1. POST /api/auth/send-code   {phone}           -> sends SMS / TG code
  2. POST /api/auth/sign-in     {phone, code}      -> verifies the code
  3. POST /api/auth/sign-in-2fa {password}          -> 2FA (if required)
  4. GET  /api/auth/status                          -> check session state
"""

from __future__ import annotations

import asyncio
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

from app.core.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

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

def _make_client() -> Client:
    return Client(
        name=settings.tg_session_name,
        api_id=settings.tg_api_id,
        api_hash=settings.tg_api_hash,
        phone_number=None,          # we handle auth manually
        workdir=str(settings.session_dir),
        in_memory=False,            # persist to file
    )


def _session_file_exists() -> bool:
    p = settings.session_dir / f"{settings.tg_session_name}.session"
    return p.exists()


# ---------- routes --------------------------------------------------------

@router.get("/status")
async def auth_status():
    """Check whether a valid session already exists."""
    has_session = _session_file_exists()

    # Quick probe: try to connect with existing session
    if has_session and not _state.logged_in:
        try:
            c = _make_client()
            await c.start()
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
        except Exception as e:
            logger.debug(f"Session probe failed: {e}")
            _state.logged_in = False

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

    client = _make_client()
    await client.connect()

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

    # Remove session file
    session_file = settings.session_dir / f"{settings.tg_session_name}.session"
    if session_file.exists():
        session_file.unlink()
        logger.info(f"Session file removed: {session_file}")

    _state.logged_in = False
    _state.needs_2fa = False
    _state.phone_code_hash = ""

    return {"message": "Logged out, session removed"}
