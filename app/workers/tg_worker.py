"""Telegram media download worker.

Supports parallel chunk downloading for large files and sends
completion / failure notifications back to the originating TG chat.
"""

import asyncio
import os
import shutil
import time
from pathlib import Path

from loguru import logger
from sqlalchemy import update

from app.core.database import async_session_factory
from app.core.tg_client import get_worker_client
from app.models.task import SourceType, Task, TaskStatus
from app.services.notifier import notify_complete, notify_failed
from app.services.tg_downloader import download_tg_file
from app.core.settings import settings
from app.workers.retry_handler import schedule_retry

# Per-task throttle state for progress callbacks (task_id -> timestamp/start)
_progress_ts: dict[int, float] = {}
_progress_start: dict[int, float] = {}


async def _do_download(task_id: int):
    """Actual async download logic for TG media."""
    # ---- load task --------------------------------------------------
    async with async_session_factory() as session:
        task = await session.get(Task, task_id)
        if not task:
            logger.error(f"Task #{task_id} not found")
            return

        if task.status not in (TaskStatus.PENDING, TaskStatus.RETRYING):
            logger.warning(f"Task #{task_id} skipped, status={task.status}")
            return

        task.status = TaskStatus.DOWNLOADING
        await session.commit()

        # Copy fields we need outside session scope
        file_id = task.telegram_file_id
        file_name = task.file_name
        file_size = task.file_size or 0
        source_type = task.source_type
        chat_id = task.telegram_chat_id
        message_id = task.telegram_message_id

    logger.info(f"Starting TG download for task #{task_id}: {file_name}")

    # ---- paths ------------------------------------------------------
    type_dir_map = {
        SourceType.TG_VIDEO: "telegram/video",
        SourceType.TG_DOCUMENT: "telegram/document",
        SourceType.TG_PHOTO: "telegram/photo",
        SourceType.TG_AUDIO: "telegram/audio",
    }
    sub_dir = type_dir_map.get(SourceType(source_type))
    if sub_dir is None:
        raise ValueError(f"Unsupported source type: {source_type}")
    target_dir = settings.storage_path / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = settings.temp_path
    temp_dir.mkdir(parents=True, exist_ok=True)

    temp_file = temp_dir / f"{file_name}.tmp"
    final_file = target_dir / file_name

    # ---- download ---------------------------------------------------
    try:
        client = await get_worker_client()
        if client is None:
            raise RuntimeError("No Pyrogram client available – session missing or not configured")

        start_time = time.time()
        _progress_start[task_id] = start_time
        _progress_ts[task_id] = 0.0

        # Use parallel downloader (auto-fallback for small files)
        await download_tg_file(
            client=client,
            file_id_str=file_id,
            file_size=file_size,
            dest_path=temp_file,
            progress=lambda cur, tot: _progress_callback(
                task_id, cur, tot),
        )

        elapsed = time.time() - start_time
        _progress_ts.pop(task_id, None)
        _progress_start.pop(task_id, None)

        # Move temp -> final
        if temp_file.exists():
            shutil.move(str(temp_file), str(final_file))
        else:
            raise RuntimeError("Downloaded temp file not found")

        actual_size = os.path.getsize(str(final_file))
        if actual_size <= 0:
            raise RuntimeError("Downloaded file is empty (0 bytes)")
        if file_size > 0 and actual_size != file_size:
            raise RuntimeError(
                f"Downloaded file size mismatch: expected {file_size}, got {actual_size}"
            )
        speed = actual_size / elapsed if elapsed > 0 else 0

        # ---- mark completed -----------------------------------------
        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=TaskStatus.COMPLETED,
                    local_path=str(final_file),
                    file_size=actual_size,
                    downloaded_size=actual_size,
                    speed=speed,
                )
            )
            await session.commit()

        logger.info(
            f"Task #{task_id} completed: {final_file} "
            f"({actual_size} bytes, {speed:.0f} B/s)"
        )

        # ---- notify TG chat -----------------------------------------
        await notify_complete(
            chat_id=chat_id,
            message_id=message_id,
            file_name=file_name,
            file_size=actual_size,
            speed=speed,
            local_path=str(final_file),
        )

    except Exception as e:
        logger.error(f"Task #{task_id} failed: {e}")

        _progress_ts.pop(task_id, None)
        _progress_start.pop(task_id, None)

        # Clean up temp files (.tmp from parallel, .tmp.temp from Pyrogram single-stream)
        temp_file.unlink(missing_ok=True)
        Path(str(temp_file) + ".temp").unlink(missing_ok=True)
        final_file.unlink(missing_ok=True)

        # Schedule retry & persist error
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.error_message = str(e)
                schedule_retry(session, task)
                await session.commit()

                await notify_failed(
                    chat_id=chat_id,
                    message_id=message_id,
                    file_name=file_name,
                    error=str(e),
                    retry_count=task.retry_count,
                    max_retries=task.max_retries,
                )


def _progress_callback(task_id: int, current: int, total: int):
    """Write download progress to DB (throttled to at most once per 3 s)."""
    now = time.time()
    last = _progress_ts.get(task_id, 0.0)
    if now - last < 3.0:
        return
    _progress_ts[task_id] = now

    elapsed = now - _progress_start.get(task_id, now)
    speed = current / elapsed if elapsed > 0 else 0.0

    async def _write():
        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(downloaded_size=current, speed=speed)
            )
            await session.commit()

    # Schedule the coroutine on the running event loop (we are inside it).
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_write())
    except Exception:
        pass


def download_tg_media(task_id: int):
    """Entry point for RQ worker (sync wrapper for async logic)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_download(task_id))
    finally:
        loop.close()
