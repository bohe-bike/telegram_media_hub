"""Telegram notification service.

After a download completes (or fails), reply to the original chat message
so the user gets instant feedback without checking the Web UI.
"""

from __future__ import annotations

import random

from loguru import logger
from pyrogram import raw
from pyrogram.utils import get_channel_id

from app.core.tg_client import get_worker_client
from app.core.settings import settings


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
    await _send_reply(chat_id, message_id, text)


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
    await _send_reply(chat_id, message_id, text)


async def _send_reply(chat_id: int, message_id: int, text: str) -> None:
    """Send a reply using the shared worker Pyrogram client."""
    client = await get_worker_client()
    if client is None:
        logger.debug("No TG client available – skipping notification")
        return

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
                f"Raw peer send failed for chat {chat_id}, falling back to send_message: {e}"
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
                f"Failed to send TG notification to chat {chat_id} after peer resolve: {retry_e}"
            )
        logger.warning(f"Failed to send TG notification to chat {chat_id}: {e}")


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
