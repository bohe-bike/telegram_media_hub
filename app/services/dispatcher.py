"""Task dispatcher: creates tasks in DB and enqueues them to Redis."""

from loguru import logger
from sqlalchemy import select

from app.core.database import async_session_factory
from app.core.redis import external_download_queue, tg_download_queue
from app.models.task import SourceType, Task, TaskStatus
from app.core.settings import settings


class TaskDispatcher:
    """Dispatches download tasks to the appropriate queue."""

    async def create_tg_download_task(
        self,
        source_type: str,
        file_id: str,
        file_name: str,
        file_size: int,
        chat_id: int,
        message_id: int,
    ) -> Task:
        """Create a TG media download task."""
        async with async_session_factory() as session:
            task = Task(
                source_type=SourceType(source_type),
                telegram_file_id=file_id,
                file_name=file_name,
                file_size=file_size,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                status=TaskStatus.PENDING,
                max_retries=settings.max_retries,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)

            # Enqueue to Redis
            tg_download_queue.enqueue(
                "app.workers.tg_worker.download_tg_media",
                task.id,
                job_timeout="2h",
            )
            logger.info(f"Created TG download task #{task.id}: {file_name}")
            return task

    async def create_external_download_task(
        self,
        source_url: str,
        chat_id: int | None = None,
        message_id: int | None = None,
    ) -> Task:
        """Create an external link download task."""
        async with async_session_factory() as session:
            # Check for duplicate URL tasks that are still active
            result = await session.execute(
                select(Task).where(
                    Task.source_url == source_url,
                    Task.status.in_([
                        TaskStatus.PENDING,
                        TaskStatus.DOWNLOADING,
                        TaskStatus.RETRYING,
                    ]),
                )
            )
            existing_task = result.scalar_one_or_none()
            if existing_task:
                logger.warning(f"Duplicate active task for URL: {source_url}")
                return existing_task

            task = Task(
                source_type=SourceType.EXTERNAL_LINK,
                source_url=source_url,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                status=TaskStatus.PENDING,
                max_retries=settings.max_retries,
            )
            session.add(task)
            await session.commit()
            await session.refresh(task)

            # Enqueue to Redis
            external_download_queue.enqueue(
                "app.workers.external_worker.download_external",
                task.id,
                job_timeout="4h",
            )
            logger.info(
                f"Created external download task #{task.id}: {source_url}")
            return task

    async def retry_task(self, task_id: int) -> Task | None:
        """Manually retry a failed task."""
        async with async_session_factory() as session:
            task = await session.get(Task, task_id)
            if not task:
                return None

            if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
                logger.warning(
                    f"Task #{task_id} is not in a retryable state: {task.status}")
                return task

            task.status = TaskStatus.RETRYING
            task.retry_count = +1
            task.error_message = None
            await session.commit()
            await session.refresh(task)

            # Re-enqueue based on source type
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

            logger.info(f"Retrying task #{task_id}")
            return task
