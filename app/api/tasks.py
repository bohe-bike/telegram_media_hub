"""FastAPI REST API routes for task management."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.task import SourceType, Task, TaskStatus
from app.schemas.task import TaskCreate, TaskListResponse, TaskResponse, TaskRetryResponse
from app.services.dispatcher import TaskDispatcher

router = APIRouter(prefix="/tasks", tags=["tasks"])
dispatcher = TaskDispatcher()


@router.post("/", response_model=TaskResponse, status_code=201)
async def create_task(
    task_in: TaskCreate,
    session: AsyncSession = Depends(get_session),
):
    """Create a new download task."""
    if task_in.source_type == SourceType.EXTERNAL_LINK:
        if not task_in.source_url:
            raise HTTPException(
                status_code=400, detail="source_url is required for external links")
        task = await dispatcher.create_external_download_task(
            source_url=task_in.source_url,
            chat_id=task_in.telegram_chat_id,
            message_id=task_in.telegram_message_id,
        )
    else:
        if not task_in.telegram_file_id:
            raise HTTPException(
                status_code=400, detail="telegram_file_id is required for TG media")
        task = await dispatcher.create_tg_download_task(
            source_type=task_in.source_type.value,
            file_id=task_in.telegram_file_id,
            file_name=task_in.file_name or "unnamed",
            file_size=0,
            chat_id=task_in.telegram_chat_id or 0,
            message_id=task_in.telegram_message_id or 0,
        )
    return task


@router.get("/", response_model=TaskListResponse)
async def list_tasks(
    status: Optional[TaskStatus] = Query(None, description="Filter by status"),
    source_type: Optional[SourceType] = Query(
        None, description="Filter by source type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
):
    """List all tasks with optional filtering."""
    query = select(Task)
    count_query = select(func.count(Task.id))

    if status:
        query = query.where(Task.status == status)
        count_query = count_query.where(Task.status == status)
    if source_type:
        query = query.where(Task.source_type == source_type)
        count_query = count_query.where(Task.source_type == source_type)

    query = query.order_by(Task.created_at.desc()).offset(offset).limit(limit)

    result = await session.execute(query)
    tasks = result.scalars().all()

    total_result = await session.execute(count_query)
    total = total_result.scalar_one()

    return TaskListResponse(total=total, tasks=[TaskResponse.model_validate(t) for t in tasks])


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get a specific task by ID."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{task_id}/retry", response_model=TaskRetryResponse)
async def retry_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Manually retry a failed task."""
    task = await dispatcher.retry_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status not in (TaskStatus.RETRYING,):
        return TaskRetryResponse(
            id=task.id,
            status=task.status,
            message=f"Task is not in a retryable state (current: {task.status})",
        )

    return TaskRetryResponse(
        id=task.id,
        status=task.status,
        message="Task has been re-queued for retry",
    )


@router.delete("/{task_id}", status_code=204)
async def delete_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Delete a task."""
    task = await session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # If task is actively downloading, mark as cancelled first
    if task.status in (TaskStatus.DOWNLOADING, TaskStatus.PENDING, TaskStatus.RETRYING):
        task.status = TaskStatus.CANCELLED
        await session.commit()

    await session.delete(task)
    await session.commit()


@router.get("/stats/summary")
async def get_stats(
    session: AsyncSession = Depends(get_session),
):
    """Get task statistics summary."""
    stats = {}
    for status in TaskStatus:
        result = await session.execute(
            select(func.count(Task.id)).where(Task.status == status)
        )
        stats[status.value] = result.scalar_one()

    total_size_result = await session.execute(
        select(func.sum(Task.file_size)).where(
            Task.status == TaskStatus.COMPLETED)
    )
    total_size = total_size_result.scalar_one() or 0

    return {
        "task_counts": stats,
        "total_downloaded_bytes": total_size,
        "total_downloaded_gb": round(total_size / (1024**3), 2),
    }
