"""Shared Pyrogram client for worker processes.

Each RQ worker process creates **one** Pyrogram client that is lazily started
and then reused for all operations (download + notifications) within that
process lifetime.  This avoids the overhead of establishing a new MTProto
connection and authentication handshake for every single task.

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
from pyrogram import Client

from app.core.settings import settings

_worker_client: Client | None = None
_lock = asyncio.Lock()


async def get_worker_client() -> Client | None:
    """Return a started, shared Pyrogram client for this worker process.

    Returns ``None`` when credentials or session are not available (in which
    case the caller should skip TG operations gracefully).
    """
    global _worker_client

    if not settings.tg_api_id or not settings.tg_api_hash:
        return None

    # Prefer session string from Redis (in-memory, avoids SQLite file lock
    # with the main listener process).  Fall back to session file if Redis
    # key is not yet populated (e.g. listener hasn't started yet).
    from app.core.redis import redis_conn
    session_str: str | None = None
    raw = redis_conn.get("tg:session_string")
    if raw:
        session_str = raw.decode() if isinstance(raw, bytes) else raw

    if not session_str:
        session_file = settings.session_dir / f"{settings.tg_session_name}.session"
        if not session_file.exists():
            return None

    async with _lock:
        if _worker_client is None:
            if session_str:
                _worker_client = Client(
                    name=":memory:",
                    api_id=settings.tg_api_id,
                    api_hash=settings.tg_api_hash,
                    session_string=session_str,
                    proxy=settings.tg_proxy,
                )
            else:
                _worker_client = Client(
                    name=settings.tg_session_name,
                    api_id=settings.tg_api_id,
                    api_hash=settings.tg_api_hash,
                    workdir=str(settings.session_dir),
                    proxy=settings.tg_proxy,
                )
            await _worker_client.start()
            logger.info("Shared worker Pyrogram client started (in-memory session).")
        elif not _worker_client.is_connected:
            try:
                await _worker_client.start()
                logger.info("Shared worker Pyrogram client reconnected.")
            except Exception as exc:
                logger.warning(f"Failed to reconnect worker client: {exc}")
                _worker_client = None
                return None

    return _worker_client
