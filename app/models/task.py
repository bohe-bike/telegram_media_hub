"""Database models for tasks and proxies."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class SourceType(str, enum.Enum):
    TG_VIDEO = "tg_video"
    TG_DOCUMENT = "tg_document"
    TG_PHOTO = "tg_photo"
    TG_AUDIO = "tg_audio"
    EXTERNAL_LINK = "external_link"


class ProxyStatus(str, enum.Enum):
    ACTIVE = "active"
    FAILED = "failed"
    DISABLED = "disabled"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(Enum(SourceType), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum(TaskStatus), nullable=False, default=TaskStatus.PENDING, index=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=5)
    proxy_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    downloaded_size: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Task(id={self.id}, type={self.source_type}, status={self.status})>"


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True)
    proxy_url: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        Enum(ProxyStatus), nullable=False, default=ProxyStatus.ACTIVE
    )
    latency: Mapped[float | None] = mapped_column(Float, nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Proxy(id={self.id}, url={self.proxy_url}, status={self.status})>"
