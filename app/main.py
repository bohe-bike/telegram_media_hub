"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.auth import router as auth_router
from app.api.config import router as config_router
from app.api.tasks import router as tasks_router
from app.core.database import init_db
from app.services.telegram import TelegramListener
from app.workers.retry_handler import recover_interrupted_tasks, recover_pending_tasks
from config.settings import settings

tg_listener = TelegramListener()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup and shutdown."""
    logger.info("=" * 60)
    logger.info("Telegram Media Hub starting up...")
    logger.info("=" * 60)

    # Initialize database tables
    await init_db()
    logger.info("Database initialized.")

    # Ensure storage directories exist
    for sub in [
        "telegram/video",
        "telegram/document",
        "telegram/photo",
        "telegram/audio",
        "external/youtube",
        "external/tiktok",
        "external/bilibili",
        "external/twitter",
        "external/other",
        "temp",
    ]:
        (settings.storage_path / sub).mkdir(parents=True, exist_ok=True)
    logger.info(f"Storage directories ready at {settings.storage_root}")

    # Recover interrupted tasks from previous run
    await recover_interrupted_tasks()
    await recover_pending_tasks()

    # Start Telegram listener (only if session exists & credentials are set)
    if settings.tg_api_id and settings.tg_api_hash:
        session_file = settings.session_dir / \
            f"{settings.tg_session_name}.session"
        if session_file.exists():
            try:
                await tg_listener.start()
            except Exception as e:
                logger.error(f"Failed to start TG listener: {e}")
                logger.warning(
                    "Continuing without TG listener. Login via Web UI.")
        else:
            logger.warning(
                "No TG session found. Please login via Web UI -> TG Login tab."
            )
    else:
        logger.warning(
            "TG credentials not configured. Set them in Web UI -> Settings.")

    yield

    # Shutdown
    logger.info("Shutting down...")
    await tg_listener.stop()


app = FastAPI(
    title="Telegram Media Hub",
    description="Self-hosted Telegram media download hub",
    version="1.0.0",
    lifespan=lifespan,
)

# ---- API routes ----
app.include_router(tasks_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(auth_router, prefix="/api")

# ---- Static files (CSS/JS/images if any) ----
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---- Serve frontend SPA ----
@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "telegram-media-hub"}
