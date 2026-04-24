"""Telegram notification service.

After a download completes (or fails), reply to the original chat message
so the user gets instant feedback without checking the Web UI.
"""

from __future__ import annotations

from loguru import logger

from app.core.tg_client import get_worker_client
from config.settings import settings


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
        f"**Download Complete**\n"
        f"File: `{file_name or 'unknown'}`\n"
        f"Size: {_fmt_bytes(file_size)}\n"
        f"Speed: {_fmt_speed(speed)}\n"
        f"Path: `{local_path or '--'}`"
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
        status = f"Retrying ({retry_count}/{max_retries})..."
    else:
        status = "All retries exhausted"

    text = (
        f"**Download Failed**\n"
        f"File: `{file_name or 'unknown'}`\n"
        f"Error: `{(error or 'unknown')[:200]}`\n"
        f"Status: {status}"
    )
    await _send_reply(chat_id, message_id, text)


async def _send_reply(chat_id: int, message_id: int, text: str) -> None:
    """Send a reply using the shared worker Pyrogram client."""
    client = await get_worker_client()
    if client is None:
        logger.debug("No TG client available – skipping notification")
        return

    try:
        await client.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=message_id,
        )
    except Exception as e:
        # Notification failure should never crash the worker
        logger.warning(
            f"Failed to send TG notification to chat {chat_id}: {e}")
