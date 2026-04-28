"""Database connection and session management."""

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.settings import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    """Dependency for FastAPI routes."""
    async with async_session_factory() as session:
        yield session


async def init_db():
    """Create all tables if Alembic hasn't been run yet.

    If the ``alembic_version`` table exists (indicating Alembic manages the
    schema), this is a no-op.  Otherwise it creates tables directly — suitable
    for development / first-time setup.
    """
    async with engine.connect() as conn:
        # Check if Alembic has ever applied a migration
        has_alembic = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).has_table("alembic_version")
        )
        if has_alembic:
            import logging
            logging.getLogger(__name__).info(
                "Alembic migration detected — skipping init_db() table creation."
            )
            return

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
