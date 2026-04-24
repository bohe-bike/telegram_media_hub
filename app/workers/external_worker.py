"""External link download worker using yt-dlp.

Sends completion / failure notifications back to the originating TG chat.
"""

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

from loguru import logger
from sqlalchemy import update

from app.core.database import async_session_factory
from app.models.task import Task, TaskStatus
from app.services.notifier import notify_complete, notify_failed
from app.workers.retry_handler import schedule_retry
from config.settings import settings


def _get_platform_dir(url: str) -> str:
    """Determine subdirectory based on URL platform."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "external/youtube"
    elif "tiktok.com" in url_lower or "vm.tiktok.com" in url_lower:
        return "external/tiktok"
    elif "bilibili.com" in url_lower or "b23.tv" in url_lower:
        return "external/bilibili"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "external/twitter"
    else:
        return "external/other"


def _build_ytdlp_command(url: str, output_path: str, proxy: str | None = None) -> list[str]:
    """Build yt-dlp command with appropriate flags."""
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "-f", settings.ytdlp_format,
        "--merge-output-format", "mp4",
        "--continue",
        "--retries", "3",
        "--fragment-retries", "3",
        "--no-overwrites",
        "-o", output_path,
    ]

    if settings.ytdlp_use_aria2:
        cmd.extend([
            "--downloader", "aria2c",
            "--downloader-args", "aria2c:-x 16 -s 16 -k 1M",
        ])

    if proxy:
        cmd.extend(["--proxy", proxy])

    cmd.append(url)
    return cmd


async def _do_download(task_id: int):
    """Actual download logic for external links."""
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

        source_url = task.source_url
        chat_id = task.telegram_chat_id
        message_id = task.telegram_message_id
        logger.info(
            f"Starting external download for task #{task_id}: {source_url}")

    # Determine storage path
    sub_dir = _get_platform_dir(source_url)
    target_dir = settings.storage_path / sub_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = settings.temp_path
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Output template: temp dir first, then move
    temp_output = str(temp_dir / "%(title)s.%(ext)s")

    # Select proxy if available
    proxy = None
    proxy_list = settings.proxy_list
    if proxy_list:
        # Simple round-robin; V2 will use smart proxy selection
        proxy = proxy_list[task_id % len(proxy_list)]

    cmd = _build_ytdlp_command(source_url, temp_output, proxy)

    try:
        start_time = time.time()

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=7200,  # 2 hour timeout
        )

        if process.returncode != 0:
            raise RuntimeError(
                f"yt-dlp exited with code {process.returncode}: {process.stderr[-500:]}"
            )

        elapsed = time.time() - start_time

        # Find the downloaded file (yt-dlp may have named it differently)
        downloaded_files = list(temp_dir.glob("*"))
        # Filter out .part files and other temp artifacts
        actual_files = [
            f for f in downloaded_files
            if f.is_file() and not f.suffix in (".part", ".tmp", ".ytdl")
        ]

        if not actual_files:
            raise RuntimeError("No output file found after yt-dlp completed")

        # Move the most recently modified file
        actual_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        downloaded_file = actual_files[0]
        final_file = target_dir / downloaded_file.name
        downloaded_file.rename(final_file)

        file_size = os.path.getsize(str(final_file))
        speed = file_size / elapsed if elapsed > 0 else 0

        async with async_session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status=TaskStatus.COMPLETED,
                    local_path=str(final_file),
                    file_name=final_file.name,
                    file_size=file_size,
                    downloaded_size=file_size,
                    speed=speed,
                    proxy_used=proxy,
                )
            )
            await session.commit()

        logger.info(
            f"Task #{task_id} completed: {final_file} "
            f"({file_size} bytes, {speed:.0f} B/s)"
        )

        # Notify TG chat
        await notify_complete(
            chat_id=chat_id,
            message_id=message_id,
            file_name=final_file.name,
            file_size=file_size,
            speed=speed,
            local_path=str(final_file),
        )

    except Exception as e:
        logger.error(f"Task #{task_id} failed: {e}")

        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if task:
                task.error_message = str(e)
                schedule_retry(session, task)
                await session.commit()

                await notify_failed(
                    chat_id=chat_id,
                    message_id=message_id,
                    file_name=task.file_name or source_url,
                    error=str(e),
                    retry_count=task.retry_count,
                    max_retries=task.max_retries,
                )


def download_external(task_id: int):
    """Entry point for RQ worker (sync wrapper for async logic)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_do_download(task_id))
    finally:
        loop.close()
