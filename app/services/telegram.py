"""Telegram client using Pyrogram for MTProto access."""

import asyncio
import re
from typing import Optional

from loguru import logger
from pyrogram import Client, filters
from pyrogram.types import Message

from app.core.settings import settings
from app.services.dispatcher import TaskDispatcher


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
        except Exception:
            pass
    import asyncio
    asyncio.ensure_future(_do())


# ------------------------------------------------------------------
# Enqueue batch notifier
# ------------------------------------------------------------------
# Per-chat accumulator: chat_id -> {"names": [...], "first_msg_id": int, "timer": Task}
_enqueue_batches: dict[int, dict] = {}


def _notify_enqueued(chat_id: int, first_msg_id: int, name: str, client: "Client") -> None:
    """Add *name* to the per-chat batch and (re)start the 5-second flush timer."""
    batch = _enqueue_batches.get(chat_id)
    if batch is None:
        batch = {"names": [], "first_msg_id": first_msg_id, "timer": None}
        _enqueue_batches[chat_id] = batch
    batch["names"].append(name)

    # Cancel existing timer and restart
    if batch["timer"] and not batch["timer"].done():
        batch["timer"].cancel()
    batch["timer"] = asyncio.ensure_future(_flush_enqueue_batch(chat_id, client))


async def _flush_enqueue_batch(chat_id: int, client: "Client") -> None:
    await asyncio.sleep(5)
    batch = _enqueue_batches.pop(chat_id, None)
    if not batch:
        return
    names: list[str] = batch["names"]
    msg_id: int = batch["first_msg_id"]
    if len(names) == 1:
        text = f"⏳ {names[0]} 已加入队列"
    else:
        first = names[0]
        text = f"⏳ {first} 等 {len(names)} 个任务已加入队列"
    try:
        await client.send_message(chat_id=chat_id, text=text, reply_to_message_id=msg_id)
    except Exception as e:
        logger.warning(f"Failed to send enqueue notification to {chat_id}: {e}")

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

    @property
    def is_running(self) -> bool:
        """Return True if the underlying Pyrogram client is connected."""
        return self.client is not None and self.client.is_connected

    async def start(self):
        """Initialize and start the Telegram client."""
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

        logger.info("Starting Telegram listener...")
        await self.client.start()
        me = await self.client.get_me()
        logger.info(f"Logged in as {me.first_name} (ID: {me.id})")

        # Export session string to Redis so worker processes can connect
        # using in_memory=True, avoiding SQLite file-locking conflicts.
        try:
            from app.core.redis import redis_conn
            session_str = await self.client.export_session_string()
            redis_conn.set("tg:session_string", session_str)
            logger.info("Session string exported to Redis for workers.")
        except Exception as exc:
            logger.warning(f"Could not export session string to Redis: {exc}")

    async def stop(self):
        """Stop the Telegram client."""
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
        _notify_enqueued(message.chat.id, message.id, file_name, self.client)

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
        _notify_enqueued(message.chat.id, message.id, file_name, self.client)

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
        _notify_enqueued(message.chat.id, message.id, file_name, self.client)

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
            _notify_enqueued(message.chat.id, message.id, url, self.client)


# Module-level singleton — shared by lifespan (main.py) and config API
tg_listener = TelegramListener()
