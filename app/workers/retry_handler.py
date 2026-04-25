"""Retry handler with exponential backoff and crash recovery."""

import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.redis import external_download_queue, retry_queue, tg_download_queue
from app.core.settings import settings
from app.models.task import SourceType, Task, TaskStatus


def get_retry_delay(retry_count: int) -> int:
    """Calculate exponential backoff delay in seconds.

    Retry schedule:
        1st: 30s
        2nd: 120s (2min)
        3rd: 600s (10min)
        4th: 1800s (30min)
        5th: 3600s (1h)
    """
    base = settings.retry_base_delay
    delay = base * (2 ** retry_count)
    # Cap at 1 hour
    return min(delay, 3600)


def schedule_retry(session: AsyncSession, task: Task):
    """Schedule a retry for a failed task (called within an existing session context).

    NOTE: This is a synchronous helper that modifies the task in-place.
    The caller must commit the session.
    """
    if task.retry_count >= task.max_retries:
        task.status = TaskStatus.FAILED
        logger.warning(
            f"Task #{task.id} exhausted all {task.max_retries} retries. Marked as FAILED."
        )
        return

    delay = get_retry_delay(task.retry_count)
    task.status = TaskStatus.RETRYING
    task.retry_count += 1

    # Enqueue with delay using RQ's scheduled execution
    if task.source_type == SourceType.EXTERNAL_LINK:
        retry_queue.enqueue_in(
            timedelta(seconds=delay),
            "app.workers.external_worker.download_external",
            task.id,
            job_timeout="4h",
        )
    else:
        retry_queue.enqueue_in(
            timedelta(seconds=delay),
            "app.workers.tg_worker.download_tg_media",
            task.id,
            job_timeout="2h",
        )

    logger.info(
        f"Task #{task.id} scheduled for retry #{task.retry_count} "
        f"in {delay}s (backoff)"
    )


async def recover_interrupted_tasks():
    """Recover tasks that were interrupted by a crash/restart.

    On startup, find tasks in DOWNLOADING or RETRYING state
    and re-enqueue them.
    """
    async with async_session_factory() as session:
        result = await session.execute(
            select(Task).where(
                Task.status.in_([TaskStatus.DOWNLOADING, TaskStatus.RETRYING])
            )
        )
        interrupted_tasks = result.scalars().all()

        if not interrupted_tasks:
            logger.info("No interrupted tasks to recover.")
            return

        logger.info(
            f"Recovering {len(interrupted_tasks)} interrupted tasks...")

        for task in interrupted_tasks:
            task.status = TaskStatus.RETRYING
            task.retry_count += 1
            task.error_message = "Recovered after service restart"

            if task.source_type == SourceType.EXTERNAL_LINK:
                external_download_queue.enqueue(
                    "app.workers.external_worker.download_external",
                    task.id,
                    job_timeout="4h",
                )
            else:
                tg_download_queue.enqueue(
                    "app.workers.tg_worker.download_tg_media",
                    task.id,
                    job_timeout="2h",
                )

            logger.info(
                f"Re-enqueued interrupted task #{task.id} ({task.source_type})")

        await session.commit()
        logger.info(
            f"Recovery complete: {len(interrupted_tasks)} tasks re-enqueued.")


async def recover_pending_tasks():
    """Re-enqueue any PENDING tasks that weren't picked up (e.g., Redis was flushed)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Task).where(Task.status == TaskStatus.PENDING)
        )
        pending_tasks = result.scalars().all()

        if not pending_tasks:
            return

        logger.info(f"Re-enqueuing {len(pending_tasks)} pending tasks...")

        for task in pending_tasks:
            if task.source_type == SourceType.EXTERNAL_LINK:
                external_download_queue.enqueue(
                    "app.workers.external_worker.download_external",
                    task.id,
                    job_timeout="4h",
                )
            else:
                tg_download_queue.enqueue(
                    "app.workers.tg_worker.download_tg_media",
                    task.id,
                    job_timeout="2h",
                )

        logger.info(f"Re-enqueued {len(pending_tasks)} pending tasks.")
