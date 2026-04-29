"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.auth import router as auth_router
from app.api.config import router as config_router
from app.api.proxies import router as proxies_router
from app.api.session import router as session_router
from app.api.status import router as status_router
from app.api.tasks import router as tasks_router
from app.core.auth import is_session_request, verify_api_key
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

    # Register bot commands if token is configured
    if settings.tg_bot_token:
        await _register_bot_commands()
        asyncio.create_task(_bot_polling_loop())

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
                # Allow up to 5 minutes for retries in case of AUTH_KEY_DUPLICATED
                await asyncio.wait_for(tg_listener.start(max_retries=3, retry_delay=30), timeout=300)
            except asyncio.TimeoutError:
                logger.warning(
                    "TG listener startup timed out (300s). "
                    "Server will continue without it – check proxy/network."
                )
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


async def _register_bot_commands() -> None:
    """Register bot menu commands via Bot API setMyCommands."""
    import httpx
    token = settings.tg_bot_token
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    commands = [
        {"command": "start", "description": "开始使用"},
        {"command": "status", "description": "查看队列状态"},
        {"command": "tasks", "description": "最近下载任务"},
        {"command": "retry", "description": "重试失败任务 id"},
    ]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"commands": commands})
            if resp.status_code == 200:
                logger.info("Bot commands registered: /start /status /tasks /retry")
            else:
                logger.warning(f"Failed to register bot commands: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Failed to register bot commands: {e}")


async def _bot_polling_loop() -> None:
    """Poll Telegram Bot API for incoming messages and handle commands."""
    import httpx
    token = settings.tg_bot_token
    if not token:
        return
    base = f"https://api.telegram.org/bot{token}"
    offset = 0
    # Wait a bit so the app is fully up before polling
    await asyncio.sleep(5)
    logger.info("Bot polling started")
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{base}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                if resp.status_code != 200:
                    await asyncio.sleep(5)
                    continue
                updates = resp.json().get("result", [])
        except Exception:
            await asyncio.sleep(5)
            continue

        for upd in updates:
            offset = max(offset, upd["update_id"] + 1)
            msg = upd.get("message", {})
            text = (msg.get("text") or "").strip()
            chat_id = msg.get("chat", {}).get("id")
            if not chat_id or not text:
                continue

            reply: str | None = None
            if text == "/start":
                reply = "🤖 MediaHub 机器人欢迎使用！\n\n" \
                        "我会在文件下载完成或失败时通知你。\n\n" \
                        "可用命令：\n" \
                        "/start - 开始使用\n" \
                        "/status - 查看队列状态\n" \
                        "/tasks - 最近下载任务\n" \
                        "/retry <id> - 重试失败任务"
            elif text == "/status":
                reply = await _bot_cmd_status()
            elif text == "/tasks":
                reply = await _bot_cmd_tasks()
            elif text.startswith("/retry"):
                parts = text.split(maxsplit=1)
                if len(parts) == 2:
                    reply = await _bot_cmd_retry(parts[1].strip())
                else:
                    reply = "用法：/retry <任务ID>"
            else:
                continue  # ignore non-command messages

            if reply:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"{base}/sendMessage",
                            json={
                                "chat_id": chat_id,
                                "text": reply,
                                "disable_web_page_preview": True,
                            },
                        )
                except Exception as e:
                    logger.warning(f"Bot failed to reply to /{text}: {e}")


# ── Bot command handlers ──────────────────────────────────────────────

async def _bot_cmd_status() -> str:
    """Return current download queue status."""
    try:
        from app.core.redis import tg_download_queue, external_download_queue, retry_queue
        tg = len(tg_download_queue)
        ext = len(external_download_queue)
        rt = len(retry_queue)
        return (
            f"📊 队列状态\n\n"
            f"🔹 TG 下载队列：{tg}\n"
            f"🔹 外部下载队列：{ext}\n"
            f"🔹 重试队列：{rt}\n"
            f"📦 总计排队：{tg + ext + rt}"
        )
    except Exception as e:
        return f"获取状态失败：{e}"


async def _bot_cmd_tasks() -> str:
    """Return the last 5 completed / downloading tasks."""
    try:
        from sqlalchemy import select
        from app.core.database import async_session_factory
        from app.models.task import Task
        async with async_session_factory() as session:
            result = await session.execute(
                select(Task)
                .order_by(Task.created_at.desc())
                .limit(5)
            )
            tasks = result.scalars().all()
        if not tasks:
            return "暂无任务记录"
        lines = ["📋 最近 5 个任务："]
        for t in tasks:
            icon = {"completed": "✅", "downloading": "⬇️", "failed": "❌",
                    "pending": "⏳", "retrying": "🔄", "cancelled": "🚫"}.get(
                t.status.value if hasattr(t.status, 'value') else str(t.status), "❓")
            name = (t.file_name or t.source_url or str(t.id))[:40]
            lines.append(f"{icon} #{t.id} {name} ({t.status})")
        return "\n".join(lines)
    except Exception as e:
        return f"获取任务失败：{e}"


async def _bot_cmd_retry(task_id_str: str) -> str:
    """Retry a failed task by its ID."""
    try:
        task_id = int(task_id_str)
    except ValueError:
        return f"无效的任务 ID：{task_id_str}"
    try:
        from app.services.dispatcher import TaskDispatcher
        dispatcher = TaskDispatcher()
        task = await dispatcher.retry_task(task_id)
        if task is None:
            return f"任务 #{task_id} 不存在"
        return f"✅ 任务 #{task_id} 已重新加入队列"
    except Exception as e:
        return f"重试失败：{e}"


app = FastAPI(
    title="Telegram Media Hub",
    description="Self-hosted Telegram media download hub",
    version="1.0.0",
    lifespan=lifespan,
)

# ---- API routes ----
app.include_router(session_router, prefix="/api")
app.include_router(tasks_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(config_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(auth_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(proxies_router, prefix="/api", dependencies=[Depends(verify_api_key)])
app.include_router(status_router, prefix="/api", dependencies=[Depends(verify_api_key)])

# ---- Static files (CSS/JS/images if any) ----
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---- Serve frontend SPA ----
@app.get("/")
async def serve_index(request: Request):
    if not is_session_request(request):
        return RedirectResponse(url="/login", status_code=303)
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/login")
async def serve_login(request: Request):
    if is_session_request(request):
        return RedirectResponse(url="/", status_code=303)
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "telegram-media-hub"}
