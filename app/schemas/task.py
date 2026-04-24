"""Pydantic schemas for API request/response."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.task import SourceType, TaskStatus


class TaskCreate(BaseModel):
    source_type: SourceType
    source_url: Optional[str] = None
    telegram_file_id: Optional[str] = None
    telegram_chat_id: Optional[int] = None
    telegram_message_id: Optional[int] = None
    file_name: Optional[str] = None


class TaskResponse(BaseModel):
    id: int
    source_type: SourceType
    source_url: Optional[str] = None
    telegram_file_id: Optional[str] = None
    file_name: Optional[str] = None
    status: TaskStatus
    retry_count: int
    proxy_used: Optional[str] = None
    speed: Optional[float] = None
    file_size: Optional[int] = None
    downloaded_size: Optional[int] = None
    local_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    total: int
    tasks: list[TaskResponse]


class TaskRetryResponse(BaseModel):
    id: int
    status: TaskStatus
    message: str
