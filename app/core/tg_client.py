"""Shared Pyrogram client for worker processes.

Each RQ worker process creates **one** Pyrogram client that is lazily started
and then reused for all operations (download + notifications) within that
process lifetime.  This avoids the overhead of establishing a new MTProto
connection and authentication handshake for every single task.

IMPORTANT — Worker MUST NEVER open the same ``.session`` SQLite file as the
listener process.  Doing so triggers ``AUTH_KEY_DUPLICATED`` on Telegram's
side, invalidating the session for all processes.

The worker always uses an **in-memory** session constructed from the session
string exported to Redis by the listener.  If the Redis key isn't populated
yet (e.g. listener is still starting up), the worker waits up to 60 seconds
before giving up.

Usage::

    from app.core.tg_client import get_worker_client

    async def my_task():
        client = await get_worker_client()
        await client.send_message(...)

The client is *not* stopped between tasks.  The process itself handles the
lifecycle; if the process exits or is killed the connection is cleaned up by
the OS / Telegram's server-side idle timeout.
"""

from __future__ import annotations

import asyncio

from loguru import logger
from pyrogram import Client, raw

from app.core.settings import settings

# Maximum time to wait for the listener to export a session string to Redis.
_WAIT_FOR_SESSION_TIMEOUT = 60  # seconds

_worker_client: Client | None = None
_lock = asyncio.Lock()


async def _wait_for_session_string() -> str | None:
    """Poll Redis until the listener has exported ``tg:session_string``.

    Returns ``None`` after timeout — caller should return gracefully.
    """
    from app.core.redis import redis_conn

    deadline = asyncio.get_event_loop().time() + _WAIT_FOR_SESSION_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        raw_val = redis_conn.get("tg:session_string")
        if raw_val:
            return raw_val.decode() if isinstance(raw_val, bytes) else raw_val
        await asyncio.sleep(1)
    return None


async def get_worker_client() -> Client | None:
    """Return a started, shared Pyrogram client for this worker process.

    Returns ``None`` when credentials or session are not available (in which
    case the caller should skip TG operations gracefully).
    """
    global _worker_client

    if not settings.tg_api_id or not settings.tg_api_hash:
        return None

    # Never open the .session file directly — always use the Redis session
    # string that the listener exported.  Wait a reasonable time for the
    # listener to finish startup.
    session_str = await _wait_for_session_string()
    if not session_str:
        logger.warning(
            "No TG session string in Redis after {}s wait – "
            "worker cannot connect. Is the listener running?",
            _WAIT_FOR_SESSION_TIMEOUT,
        )
        return None

    async with _lock:
        if _worker_client is None:
            _worker_client = Client(
                name=":memory:",
                api_id=settings.tg_api_id,
                api_hash=settings.tg_api_hash,
                session_string=session_str,
                proxy=settings.tg_proxy,
            )
            await _worker_client.start()
            logger.info("Shared worker Pyrogram client started (in-memory session).")
        elif not _worker_client.is_connected:
            # Re-fetch session string from Redis — the listener may have
            # refreshed it since initial connect.
            fresh_str = await _wait_for_session_string()
            if not fresh_str:
                logger.warning("Cannot reconnect worker: no session string in Redis.")
                _worker_client = None
                return None

            # Replace the old client with a fresh one using the latest string.
            try:
                await _worker_client.disconnect()
            except Exception:
                pass
            _worker_client = Client(
                name=":memory:",
                api_id=settings.tg_api_id,
                api_hash=settings.tg_api_hash,
                session_string=fresh_str,
                proxy=settings.tg_proxy,
            )
            try:
                await _worker_client.start()
                logger.info("Shared worker Pyrogram client reconnected with fresh session string.")
            except Exception as exc:
                _is_auth = "auth key" in str(exc).lower() or "transport error: 404" in str(exc).lower()
                if _is_auth:
                    logger.error(f"Worker client auth key invalid, clearing session: {exc}")
                    try:
                        from app.core.redis import redis_conn as _rc
                        _rc.delete("tg:session_string")
                    except Exception:
                        pass
                else:
                    logger.warning(f"Failed to reconnect worker client: {exc}")
                _worker_client = None
                return None
        else:
            # Connection appears up, but verify with a lightweight ping
            # to catch silent disconnects (e.g. Telegram idle timeout).
            try:
                await asyncio.wait_for(
                    _worker_client.invoke(raw.functions.Ping(ping=0)),
                    timeout=5.0,
                )
            except Exception:
                logger.debug("Main worker client ping failed, attempting reconnect")
                # Same re-fetch + reconnect flow as above
                fresh_str = await _wait_for_session_string()
                if not fresh_str:
                    logger.warning("Cannot reconnect worker after ping failure: no session string in Redis.")
                    _worker_client = None
                    return None
                try:
                    await _worker_client.disconnect()
                except Exception:
                    pass
                _worker_client = Client(
                    name=":memory:",
                    api_id=settings.tg_api_id,
                    api_hash=settings.tg_api_hash,
                    session_string=fresh_str,
                    proxy=settings.tg_proxy,
                )
                try:
                    await _worker_client.start()
                    logger.info("Shared worker Pyrogram client reconnected after ping failure.")
                except Exception as exc:
                    logger.warning(f"Failed to reconnect worker client after ping: {exc}")
                    _worker_client = None
                    return None

    return _worker_client


async def export_session_to_redis(client: Client) -> bool:
    """Export the current Pyrogram client's session string to Redis.

    Called by:
    - ``TelegramListener.start()`` after initial login
    - ``TelegramListener._session_refresh_loop()`` periodically
    - ``auth.py`` after successful sign-in / 2FA

    Returns ``True`` on success, ``False`` on failure.
    """
    try:
        from app.core.redis import redis_conn
        session_str = await client.export_session_string()
        redis_conn.set("tg:session_string", session_str)
        return True
    except Exception as exc:
        logger.warning(f"Failed to export session string to Redis: {exc}")
        return False
