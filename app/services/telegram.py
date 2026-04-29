"""Telegram client using Pyrogram for MTProto access."""

import asyncio
import re
from typing import Optional

from loguru import logger
from pyrogram import Client, filters
from pyrogram.types import Message

from app.core.tg_client import export_session_to_redis
from app.core.settings import settings
from app.services.dispatcher import TaskDispatcher
from app.services.notifier import notify_enqueued


def _is_auth_key_error(exc: BaseException) -> bool:
    """Return True if *exc* indicates the session's auth key is no longer valid."""
    msg = str(exc).lower()
    return (
        "auth_key" in msg
        or "auth key" in msg
        or "authorization key" in msg
        or "transport error: 404" in msg
    )


def _clear_session() -> None:
    """Delete the stale .session file and remove the Redis session string."""
    try:
        session_file = settings.session_dir / f"{settings.tg_session_name}.session"
        session_file.unlink(missing_ok=True)
        logger.info("Stale session file removed.")
    except Exception:
        pass
    try:
        from app.core.redis import redis_conn
        redis_conn.delete("tg:session_string")
        redis_conn.delete("tg:session_gen")
        logger.info("Redis session string and generation cleared.")
    except Exception:
        pass


def _cache_peer(message: Message) -> None:
    """Store the chat's access_hash in Redis so workers can send notifications
    without needing a local peer cache (in-memory sessions lack one)."""
    async def _do():
        try:
            from app.core.redis import redis_conn
            peer = await message._client.resolve_peer(message.chat.id)  # type: ignore[attr-defined]
            if hasattr(peer, 'access_hash'):
                redis_conn.set(f"tg:peer_hash:{message.chat.id}", str(peer.access_hash))
                redis_conn.set(f"tg:peer_type:{message.chat.id}", type(peer).__name__)
        except Exception as exc:
            logger.debug(f"_cache_peer failed for chat {message.chat.id}: {exc}")

    import asyncio
    task = asyncio.ensure_future(_do())
    # Log any unhandled exception that propagates out of _do() to avoid
    # silent failures that are invisible to debugging.
    task.add_done_callback(
        lambda t: logger.warning(f"_cache_peer task raised: {t.exception()}")
        if t.exception() else None
    )


# ------------------------------------------------------------------
# Enqueue batch notifier
# ------------------------------------------------------------------
# Per-chat accumulator: chat_id -> {"names": [...], "first_msg_id": int, "timer": Task}
_enqueue_batches: dict[int, dict] = {}


def _notify_enqueued(chat_id: int, first_msg_id: int, name: str) -> None:
    """Add *name* to the per-chat batch and (re)start the 5-second flush timer.

    The flush sends one condensed notification via the configured notifier
    (user client or bot), so rapid consecutive enqueues are grouped.
    """
    batch = _enqueue_batches.get(chat_id)
    if batch is None:
        batch = {"names": [], "first_msg_id": first_msg_id, "timer": None}
        _enqueue_batches[chat_id] = batch
    batch["names"].append(name)

    # Cancel existing timer and restart
    if batch["timer"] and not batch["timer"].done():
        batch["timer"].cancel()
    batch["timer"] = asyncio.ensure_future(_flush_enqueue_batch(chat_id))


async def _flush_enqueue_batch(chat_id: int) -> None:
    await asyncio.sleep(5)
    batch = _enqueue_batches.pop(chat_id, None)
    if not batch:
        return
    names: list[str] = batch["names"]
    msg_id: int = batch["first_msg_id"]
    if len(names) == 1:
        await notify_enqueued(chat_id, msg_id, names[0])
    else:
        await notify_enqueued(chat_id, msg_id, names[0], batch_count=len(names))

# URL regex pattern for extracting links from messages
URL_PATTERN = re.compile(
    r'https?://(?:www\.)?'
    r'(?:youtube\.com/watch\?v=|youtu\.be/|'
    r'tiktok\.com/|vm\.tiktok\.com/|'
    r'bilibili\.com/video/|b23\.tv/|'
    r'twitter\.com/|x\.com/|'
    r'[\w.-]+\.[\w]{2,})'
    r'[^\s<>\"\']* ',
    re.IGNORECASE,
)

# More general URL pattern as fallback
GENERAL_URL_PATTERN = re.compile(
    r'https?://[^\s<>\"\']+',
    re.IGNORECASE,
)


class TelegramListener:
    """Listens for messages from Telegram and dispatches download tasks."""

    def __init__(self):
        self.client: Optional[Client] = None
        self.dispatcher: Optional[TaskDispatcher] = None
        self._refresh_task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        """Return True if the underlying Pyrogram client is connected."""
        return self.client is not None and self.client.is_connected

    async def start(self, max_retries: int = 3, retry_delay: float = 5.0):
        """Initialize and start the Telegram client.

        Parameters
        ----------
        max_retries : int
            Maximum number of retries on AUTH_KEY_DUPLICATED errors.
        retry_delay : float
            Seconds to wait between retries (doubles each attempt).
        """
        proxy = settings.tg_proxy
        self.client = Client(
            name=settings.tg_session_name,
            api_id=settings.tg_api_id,
            api_hash=settings.tg_api_hash,
            workdir=str(settings.session_dir),
            proxy=proxy,
        )
        if proxy:
            logger.info(f"Telegram listener using proxy: {proxy['scheme']}://{proxy['hostname']}:{proxy['port']}")
        self.dispatcher = TaskDispatcher()

        self._register_handlers()

        # Retry loop for AUTH_KEY_DUPLICATED - happens when a previous session
        # is still active on Telegram's server (e.g., container was killed).
        # Telegram will eventually drop the old connection, so we just wait and retry.
        for attempt in range(max_retries + 1):
            logger.info(
                f"Starting Telegram listener (attempt {attempt + 1}/{max_retries + 1})..."
            )
            try:
                await self.client.start()
                break  # Success
            except Exception as e:
                is_dup = "AUTH_KEY_DUPLICATED" in str(e).upper()
                if is_dup and attempt < max_retries:
                    # Telegram server still thinks old connection is alive.
                    # Wait with exponential backoff: 10s → 20s → 40s
                    wait_time = retry_delay * (2 ** attempt)
                    logger.warning(
                        f"Auth key conflict – Telegram still holding old connection. "
                        f"Waiting {wait_time}s before retry {attempt + 2}/{max_retries + 1}..."
                    )
                    await asyncio.sleep(wait_time)
                else:
                    if is_dup:
                        logger.error(
                            f"Failed to start after {max_retries + 1} attempts. "
                            f"This usually means a previous container/process didn't "
                            f"shut down cleanly. Wait a few minutes and try again."
                        )
                    # For other auth errors, clear stale session
                    if _is_auth_key_error(e):
                        logger.error(f"Auth key invalid – clearing stale session: {e}")
                        _clear_session()
                    raise

        me = await self.client.get_me()
        logger.info(f"Logged in as {me.first_name} (ID: {me.id})")

        # Export session string to Redis so worker processes can connect
        # using in_memory=True, avoiding SQLite file-locking conflicts.
        if await export_session_to_redis(self.client):
            logger.info("Session string exported to Redis for workers.")
        else:
            logger.warning("Could not export session string to Redis for workers.")

        # Start a background task that periodically refreshes the session
        # string in Redis.  This keeps the in-memory sessions used by
        # workers valid even if Telegram's server side rotates the auth key
        # or if the listener's .session file is replaced (e.g. by re-login).
        self._refresh_task = asyncio.create_task(self._session_refresh_loop())
        logger.info("Session string refresh loop started (interval: 4h).")

    async def _session_refresh_loop(self) -> None:
        """Periodically re-export the session string to Redis."""
        while True:
            await asyncio.sleep(4 * 3600)  # every 4 hours
            if not self.client or not self.client.is_connected:
                logger.debug("Listener not connected, skipping session refresh.")
                continue
            if await export_session_to_redis(self.client):
                logger.debug("Session string refreshed in Redis.")
            else:
                logger.warning("Failed to refresh session string in Redis.")

    async def stop(self):
        """Stop the Telegram client."""
        # Cancel the refresh loop first
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self.client:
            await self.client.stop()
            logger.info("Telegram listener stopped.")

    def _register_handlers(self):
        """Register message handlers."""
        monitored = settings.monitored_chat_ids

        if monitored:
            chat_filter = filters.chat(monitored)
            logger.info(f"Monitoring specific chats: {monitored}")
        else:
            chat_filter = filters.all
            logger.info("Monitoring ALL chats (no filter configured)")

        @self.client.on_message(chat_filter & filters.video)
        async def handle_video(client: Client, message: Message):
            _cache_peer(message)
            await self._handle_tg_video(message)

        @self.client.on_message(chat_filter & filters.document)
        async def handle_document(client: Client, message: Message):
            _cache_peer(message)
            await self._handle_tg_document(message)

        @self.client.on_message(chat_filter & filters.photo)
        async def handle_photo(client: Client, message: Message):
            _cache_peer(message)
            await self._handle_tg_photo(message)

        @self.client.on_message(chat_filter & filters.audio)
        async def handle_audio(client: Client, message: Message):
            _cache_peer(message)
            await self._handle_tg_audio(message)

        @self.client.on_message(chat_filter & (filters.text | filters.caption))
        async def handle_text(client: Client, message: Message):
            _cache_peer(message)
            await self._handle_text_message(message)

    async def _handle_tg_video(self, message: Message):
        """Handle Telegram native video messages."""
        video = message.video
        file_name = video.file_name or f"video_{message.id}.mp4"
        logger.info(
            f"Received TG video from chat {message.chat.id}: "
            f"{file_name} ({video.file_size} bytes)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_video",
            file_id=video.file_id,
            file_name=file_name,
            file_size=video.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )
        _notify_enqueued(message.chat.id, message.id, file_name)

    async def _handle_tg_document(self, message: Message):
        """Handle Telegram document messages."""
        doc = message.document
        file_name = doc.file_name or f"doc_{message.id}"
        logger.info(
            f"Received TG document from chat {message.chat.id}: "
            f"{file_name} ({doc.file_size} bytes)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_document",
            file_id=doc.file_id,
            file_name=file_name,
            file_size=doc.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )
        _notify_enqueued(message.chat.id, message.id, file_name)

    async def _handle_tg_photo(self, message: Message):
        """Handle Telegram photo messages."""
        photo = message.photo
        file_size = photo.file_size or 0
        file_name = f"photo_{message.id}.jpg"
        logger.info(
            f"Received TG photo from chat {message.chat.id}: "
            f"{file_name} ({file_size} bytes)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_photo",
            file_id=photo.file_id,
            file_name=file_name,
            file_size=file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )
        _notify_enqueued(message.chat.id, message.id, file_name, self.client)

    async def _handle_tg_audio(self, message: Message):
        """Handle Telegram audio messages."""
        audio = message.audio
        file_name = audio.file_name or f"audio_{message.id}.mp3"
        logger.info(
            f"Received TG audio from chat {message.chat.id}: "
            f"{file_name} ({audio.file_size} bytes, {audio.duration}s)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_audio",
            file_id=audio.file_id,
            file_name=file_name,
            file_size=audio.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )
        _notify_enqueued(message.chat.id, message.id, file_name)

    async def _handle_text_message(self, message: Message):
        """Handle text messages, extract URLs for external download.

        Only URLs matching known supported platforms are dispatched to avoid
        creating download tasks for arbitrary links (e.g., plain web pages).
        """
        text = message.text or message.caption or ""
        if not text:
            return

        # First pass: collect all URLs in the message
        all_urls = GENERAL_URL_PATTERN.findall(text)
        if not all_urls:
            return

        # Second pass: keep only URLs that match a supported platform
        supported_urls = [u for u in all_urls if URL_PATTERN.search(u)]
        if not supported_urls:
            return

        for url in supported_urls:
            url = url.strip()
            logger.info(f"Extracted supported URL from chat {message.chat.id}: {url}")
            await self.dispatcher.create_external_download_task(
                source_url=url,
                chat_id=message.chat.id,
                message_id=message.id,
            )
            _notify_enqueued(message.chat.id, message.id, url)


# Module-level singleton — shared by lifespan (main.py) and config API
tg_listener = TelegramListener()
