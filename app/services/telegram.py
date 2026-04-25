"""Telegram client using Pyrogram for MTProto access."""

import asyncio
import re
from typing import Optional

from loguru import logger
from pyrogram import Client, filters
from pyrogram.types import Message

from app.core.settings import settings
from app.services.dispatcher import TaskDispatcher

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
            await self._handle_tg_video(message)

        @self.client.on_message(chat_filter & filters.document)
        async def handle_document(client: Client, message: Message):
            await self._handle_tg_document(message)

        @self.client.on_message(chat_filter & filters.photo)
        async def handle_photo(client: Client, message: Message):
            await self._handle_tg_photo(message)

        @self.client.on_message(chat_filter & filters.audio)
        async def handle_audio(client: Client, message: Message):
            await self._handle_tg_audio(message)

        @self.client.on_message(chat_filter & (filters.text | filters.caption))
        async def handle_text(client: Client, message: Message):
            await self._handle_text_message(message)

    async def _handle_tg_video(self, message: Message):
        """Handle Telegram native video messages."""
        video = message.video
        logger.info(
            f"Received TG video from chat {message.chat.id}: "
            f"{video.file_name or 'unnamed'} ({video.file_size} bytes)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_video",
            file_id=video.file_id,
            file_name=video.file_name or f"video_{message.id}.mp4",
            file_size=video.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )

    async def _handle_tg_document(self, message: Message):
        """Handle Telegram document messages."""
        doc = message.document
        logger.info(
            f"Received TG document from chat {message.chat.id}: "
            f"{doc.file_name or 'unnamed'} ({doc.file_size} bytes)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_document",
            file_id=doc.file_id,
            file_name=doc.file_name or f"doc_{message.id}",
            file_size=doc.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )

    async def _handle_tg_photo(self, message: Message):
        """Handle Telegram photo messages."""
        photo = message.photo
        # message.photo is already the highest-resolution Photo object;
        # file_size may be None for very old photos.
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

    async def _handle_tg_audio(self, message: Message):
        """Handle Telegram audio messages."""
        audio = message.audio
        logger.info(
            f"Received TG audio from chat {message.chat.id}: "
            f"{audio.file_name or audio.title or 'unnamed'} "
            f"({audio.file_size} bytes, {audio.duration}s)"
        )
        await self.dispatcher.create_tg_download_task(
            source_type="tg_audio",
            file_id=audio.file_id,
            file_name=audio.file_name or f"audio_{message.id}.mp3",
            file_size=audio.file_size,
            chat_id=message.chat.id,
            message_id=message.id,
        )

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


# Module-level singleton — shared by lifespan (main.py) and config API
tg_listener = TelegramListener()
