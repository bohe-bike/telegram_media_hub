"""External link download worker — delegates to MeTube (yt-dlp front-end).

Flow:
  1. Validate URL (SSRF guard)
  2. POST to MeTube /add
  3. Poll GET /history every 5 s until the URL appears in the done list
  4. Update task in DB and send TG notification
"""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from loguru import logger
from sqlalchemy import update

from app.core.database import async_session_factory
from app.models.task import Task, TaskStatus
from app.services.notifier import notify_complete, notify_failed
from app.workers.retry_handler import schedule_retry
from config.settings import settings

# Security: only allow http/https; block private/loopback addresses
_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTS = re.compile(
    r"^(localhost|127\.|0\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1|fd[0-9a-f]{2}:)",
    re.IGNORECASE,
)


def _validate_url(url: str) -> None:
    """Raise ValueError if URL is unsafe (non-http/https or private addresses)."""
    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise ValueError(f"Malformed URL: {url}") from exc

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Disallowed URL scheme '{parsed.scheme}': {url}")

    host = parsed.hostname or ""
    if _BLOCKED_HOSTS.match(host):
        raise ValueError(f"Blocked private/loopback host '{host}': {url}")


def _get_metube_folder(url: str) -> str:
    """Return the MeTube subfolder name (relative to MeTube DOWNLOAD_DIR)."""
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    elif "tiktok.com" in u or "vm.tiktok.com" in u:
        return "tiktok"
    elif "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    elif "twitter.com" in u or "x.com" in u:
        return "twitter"
    else:
        return "other"


async def _submit_to_metube(url: str, folder: str) -> None:
    """POST the URL to MeTube's /add endpoint."""
    base = settings.metube_url.rstrip("/")
    if not base:
        raise RuntimeError("metube_url is not configured")

    payload = {
        "url": url,
        "quality": "best",
        "download_type": "video",
        "format": "mp4",
        "folder": folder,
        "auto_start": True,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{base}/add", json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"MeTube rejected URL: {data.get('msg', data)}")

    logger.info(f"Submitted to MeTube: {url!r} (folder={folder!r})")


async def _poll_metube_completion(url: str, timeout_sec: int = 7200) -> dict:
    """Poll MeTube GET /history until the URL appears in the done list.

    Returns the completed DownloadInfo dict on success.
    Raises RuntimeError on download error or timeout.

    URL matching is intentionally lenient (substring) to handle cases where
    MeTube normalises the URL (e.g. youtu.be short links → canonical form).
    """
    base = settings.metube_url.rstrip("/")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec

    async with httpx.AsyncClient() as client:
        while True:
            if loop.time() > deadline:
                raise RuntimeError(
                    f"MeTube download timed out after {timeout_sec}s"
                )

            try:
                resp = await client.get(f"{base}/history", timeout=30.0)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(f"MeTube /history poll error: {exc} — retrying in 5 s")
                await asyncio.sleep(5)
                continue

            for item in data.get("done", []):
                item_url = item.get("url", "")
                # Exact or substring match to cope with URL normalisation
                if item_url == url or url in item_url or item_url in url:
                    status = item.get("status")
                    if status == "finished":
                        return item
                    elif status == "error":
                        raise RuntimeError(
                            f"MeTube download failed: {item.get('msg') or 'unknown error'}"
                        )

            await asyncio.sleep(5)


async def _do_download(task_id: int) -> None:
    """Core async download logic: submit to MeTube and wait for completion."""
    # --- Read task state ---
    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if not task:
            logger.error(f"Task #{task_id} not found")
            return

        if task.status not in (TaskStatus.PENDING, TaskStatus.RETRYING):
            logger.warning(f"Task #{task_id} skipped, current status={task.status}")
            return

        source_url = task.source_url
        chat_id = task.telegram_chat_id
        message_id = task.telegram_message_id

        # Validate URL before marking as DOWNLOADING
        try:
            _validate_url(source_url)
        except ValueError as exc:
            task.status = TaskStatus.FAILED
            task.error_message = str(exc)
            await session.commit()
            logger.error(f"Task #{task_id} rejected: {exc}")
            return

        task.status = TaskStatus.DOWNLOADING
        await session.commit()

    logger.info(f"Task #{task_id}: forwarding to MeTube → {source_url}")
    folder = _get_metube_folder(source_url)

    try:
        await _submit_to_metube(source_url, folder)
        info = await _poll_metube_completion(source_url)

        filename = info.get("filename") or ""
        file_size = info.get("size") or 0

        # MeTube saves to:  DOWNLOAD_DIR/<folder>/<filename>
        #   DOWNLOAD_DIR = /downloads/external  (volume: media_data → /downloads in MeTube)
        #   App container sees:  media_data → /media
        #   → full path in app: /media/external/<folder>/<filename>
        local_path = (
            str(Path(settings.storage_root) / "external" / folder / filename)
            if filename
            else ""
        )

        if local_path and not os.path.exists(local_path):
            logger.warning(
                f"Task #{task_id}: expected file not found at {local_path!r}"
            )
            local_path = ""

        if not file_size and local_path and os.path.exists(local_path):
            file_size = os.path.getsize(local_path)

        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=TaskStatus.COMPLETED,
                    local_path=local_path or None,
                    file_name=filename or None,
                    file_size=file_size or None,
                    downloaded_size=file_size or None,
                )
            )
            await session.commit()

        logger.info(f"Task #{task_id} completed: {filename} ({file_size} bytes)")

        await notify_complete(
            chat_id=chat_id,
            message_id=message_id,
            file_name=filename or source_url,
            file_size=file_size,
            speed=0,
            local_path=local_path or "",
        )

    except Exception as exc:
        logger.error(f"Task #{task_id} failed: {exc}")

        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.error_message = str(exc)
                schedule_retry(session, task)
                await session.commit()

                await notify_failed(
                    chat_id=chat_id,
                    message_id=message_id,
                    file_name=task.file_name or source_url,
                    error=str(exc),
                    retry_count=task.retry_count,
                    max_retries=task.max_retries,
                )


def download_external(task_id: int) -> None:
    """Entry point for RQ worker (sync wrapper for async logic)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_download(task_id))
    finally:
        loop.close()
