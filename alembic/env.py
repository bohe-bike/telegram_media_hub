"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
import os
import tomllib
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.database import Base
from app.models.task import Task, Proxy  # noqa: F401 - ensure models are imported

# Load database URL from config/config.toml (falls back to env var)
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
TOML_FILE = CONFIG_DIR / "config.toml"

_toml_db_url: str | None = None
if TOML_FILE.exists():
    with open(TOML_FILE, "rb") as _f:
        _toml = tomllib.load(_f)
    # Support both flat key and nested sections
    _toml_db_url = _toml.get("database_url")
    if _toml_db_url is None:
        for _v in _toml.values():
            if isinstance(_v, dict) and "database_url" in _v:
                _toml_db_url = _v["database_url"]
                break

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL env var overrides the TOML value (useful in CI/CD / Docker)
db_url = os.environ.get("DATABASE_URL", _toml_db_url)
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
