"""Telegram notification service.

After a download completes (or fails), reply to the original chat message
so the user gets instant feedback without checking the Web UI.

Supports two modes:
- **user** (default): reply using the user's Pyrogram session (current behaviour)
- **bot**: reply via a Telegram Bot (requires ``tg_bot_token``) — messages come
  from the bot identity instead of the user's personal account.
"""

from __future__ import annotations

import random

from httpx import AsyncClient
from loguru import logger
from pyrogram import raw
from pyrogram.utils import get_channel_id

from app.core.tg_client import get_worker_client
from app.core.settings import settings

_BOT_API_BASE = "https://api.telegram.org/bot"

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def notify_complete(
    chat_id: int,
    message_id: int,
    file_name: str | None,
    file_size: int | None,
    speed: float | None,
    local_path: str | None,
) -> None:
    """Send a 'download complete' reply to the original TG message."""
    if not settings.tg_notify_on_complete:
        return
    if not chat_id:
        return

    text = (
        f"\u2705 下载完成\n"
        f"文件：{file_name or 'unknown'}\n"
        f"大小：{_fmt_bytes(file_size)}\n"
        f"速度：{_fmt_speed(speed)}"
    )
    await _send_message(chat_id, message_id, text)


async def notify_failed(
    chat_id: int,
    message_id: int,
    file_name: str | None,
    error: str | None,
    retry_count: int = 0,
    max_retries: int = 0,
) -> None:
    """Send a 'download failed' reply to the original TG message."""
    if not settings.tg_notify_on_fail:
        return
    if not chat_id:
        return

    if retry_count < max_retries:
        status = f"重试中 ({retry_count}/{max_retries})..."
    else:
        status = "已耗尽所有重试次数"

    text = (
        f"\u274c 下载失败\n"
        f"文件：{file_name or 'unknown'}\n"
        f"错误：{(error or 'unknown')[:200]}\n"
        f"状态：{status}"
    )
    await _send_message(chat_id, message_id, text)


async def notify_enqueued(
    chat_id: int,
    message_id: int,
    file_name: str,
    batch_count: int | None = None,
) -> None:
    """Send a 'task enqueued' notification.

    If *batch_count* is given the message says "xxx 等 N 个任务已加入队列",
    otherwise it's a single-task message.
    """
    if not chat_id:
        return
    if batch_count and batch_count > 1:
        text = f"\u23f3 {file_name} 等 {batch_count} 个任务已加入队列"
    else:
        text = f"\u23f3 {file_name} 已加入队列"
    await _send_message(chat_id, message_id, text)


# ---------------------------------------------------------------------------
# Unified sender — routes to bot or user client based on tg_notify_mode
# ---------------------------------------------------------------------------


async def _send_message(chat_id: int, message_id: int, text: str) -> None:
    """Send a reply via the currently configured notification mode."""
    mode = settings.tg_notify_mode
    if mode == "bot":
        ok = await _send_via_bot(chat_id, message_id, text)
        if not ok:
            logger.warning("Bot notification failed, falling back to user client")
            await _send_via_user_client(chat_id, message_id, text)
    else:
        await _send_via_user_client(chat_id, message_id, text)


# ---------------------------------------------------------------------------
# Bot API sender (httpx)
# ---------------------------------------------------------------------------


async def _send_via_bot(chat_id: int, message_id: int, text: str) -> bool:
    """Send a reply via Telegram Bot API. Returns True on success."""
    token = settings.tg_bot_token
    if not token:
        logger.debug("Bot token not configured, skipping bot notification")
        return False

    url = f"{_BOT_API_BASE}{token}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "reply_to_message_id": message_id,
    }

    try:
        async with AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            # Decode Telegram error for actionable hints
            msg = resp.text[:300]
            if "chat not found" in msg.lower() or "bot can't initiate" in msg.lower():
                logger.error(
                    "Bot cannot send to chat {}. "
                    "The bot must be added to the chat/channel AND the user must "
                    "have started a conversation with the bot (for private chats).",
                    chat_id,
                )
            elif "bot was blocked" in msg.lower():
                logger.error(
                    "Bot was blocked by user in chat {}. Unblock and try again.", chat_id
                )
            else:
                logger.warning(
                    "Bot API sendMessage failed (HTTP {}) for chat {}: {}",
                    resp.status_code, chat_id, msg,
                )
            return False
    except Exception as e:
        logger.warning("Bot API request failed for chat {}: {}", chat_id, e)
        return False


# ---------------------------------------------------------------------------
# User-client sender (Pyrogram — existing logic)
# ---------------------------------------------------------------------------


async def _warm_notifier_peer(client, chat_id: int) -> None:
    """Populate Pyrogram's in-memory peer cache for notifier sends."""
    peer = _build_peer(chat_id)
    if peer is not None:
        try:
            if isinstance(peer, raw.types.InputPeerChannel):
                inp = raw.types.InputChannel(
                    channel_id=peer.channel_id,
                    access_hash=peer.access_hash,
                )
                await client.invoke(raw.functions.channels.GetChannels(id=[inp]))
            elif isinstance(peer, raw.types.InputPeerUser):
                inp = raw.types.InputUser(
                    user_id=peer.user_id,
                    access_hash=peer.access_hash,
                )
                await client.invoke(raw.functions.users.GetUsers(id=[inp]))
        except Exception:
            pass
    try:
        await client.resolve_peer(chat_id)
    except Exception:
        pass


async def _send_via_user_client(chat_id: int, message_id: int, text: str) -> None:
    """Send a reply using the shared worker Pyrogram client."""
    client = await get_worker_client()
    if client is None:
        logger.debug("No TG client available – skipping notification")
        return

    # Warm peer cache so in-memory worker sessions can resolve IDs
    await _warm_notifier_peer(client, chat_id)

    peer = _build_peer(chat_id)

    if peer is not None:
        try:
            await client.invoke(
                raw.functions.messages.SendMessage(
                    peer=peer,
                    message=text,
                    random_id=random.getrandbits(63),
                    no_webpage=True,
                    reply_to_msg_id=message_id,
                )
            )
            return
        except Exception as e:
            logger.debug(
                "Raw peer send failed for chat {}, falling back to send_message: {}",
                chat_id,
                e,
            )

    async def _do_send(target):
        await client.send_message(
            chat_id=target,
            text=text,
            reply_to_message_id=message_id,
        )

    try:
        await _do_send(chat_id)
    except Exception as e:
        try:
            await client.get_chat(chat_id)
            await _do_send(chat_id)
            return
        except Exception as retry_e:
            logger.warning(
                "Failed to send TG notification to chat {} after peer resolve: {}",
                chat_id,
                retry_e,
            )
        logger.warning("Failed to send TG notification to chat {}: {}", chat_id, e)


def _build_peer(chat_id: int):
    """Construct a raw InputPeer from Redis-cached metadata, or None."""
    try:
        from app.core.redis import redis_conn

        raw_hash = redis_conn.get(f"tg:peer_hash:{chat_id}")
        peer_type = redis_conn.get(f"tg:peer_type:{chat_id}")
        pt = peer_type.decode() if isinstance(peer_type, bytes) else (peer_type or "")

        if "Chat" in pt and "Channel" not in pt:
            return raw.types.InputPeerChat(chat_id=abs(int(chat_id)))

        if not raw_hash:
            return None

        access_hash = int(raw_hash)
        if "Channel" in pt:
            return raw.types.InputPeerChannel(
                channel_id=get_channel_id(int(chat_id)),
                access_hash=access_hash,
            )

        return raw.types.InputPeerUser(
            user_id=abs(int(chat_id)),
            access_hash=access_hash,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_bytes(b: int | None) -> str:
    if not b:
        return "unknown"
    if b < 1024:
        return f"{b} B"
    if b < 1048576:
        return f"{b / 1024:.1f} KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1073741824:.2f} GB"


def _fmt_speed(bps: float | None) -> str:
    if not bps:
        return "--"
    if bps < 1048576:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps / 1048576:.1f} MB/s"
