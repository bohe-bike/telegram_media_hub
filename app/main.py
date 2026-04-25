"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.auth import router as auth_router
from app.api.config import router as config_router
from app.api.proxies import router as proxies_router
from app.api.status import router as status_router
from app.api.tasks import router as tasks_router
from app.core.auth import verify_api_key
from app.core.database import init_db
from app.services.proxy_pool import ProxyPool
from app.services.telegram import tg_listener
from app.core.settings import settings
from app.workers.retry_handler import recover_interrupted_tasks, recover_pending_tasks

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

    # Sync proxies from settings into DB and start background health checker
    await ProxyPool.sync_from_settings()
    proxy_health_task: asyncio.Task | None = None
    if settings.proxy_check_interval > 0:
        proxy_health_task = asyncio.create_task(_proxy_health_loop())
        logger.info(
            f"Proxy health checker started (interval: {settings.proxy_check_interval}s)."
        )

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
    if proxy_health_task:
        proxy_health_task.cancel()
    await tg_listener.stop()


async def _proxy_health_loop() -> None:
    """Background task: periodically check all proxy health."""
    # Brief initial delay so the app finishes startup before hammering proxies
    await asyncio.sleep(30)
    while True:
        try:
            await ProxyPool.check_all()
        except Exception as exc:
            logger.error(f"Proxy health check loop error: {exc}")
        await asyncio.sleep(settings.proxy_check_interval)


app = FastAPI(
    title="Telegram Media Hub",
    description="Self-hosted Telegram media download hub",
    version="1.0.0",
    lifespan=lifespan,
)

# ---- API routes ----
app.include_router(tasks_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(config_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(auth_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(proxies_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(status_router, prefix="/api", dependencies=[Depends(verify_api_key)])

# ---- Static files (CSS/JS/images if any) ----
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---- Serve frontend SPA ----
@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "telegram-media-hub"}
