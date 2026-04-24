"""Service health and configuration status endpoint."""

from fastapi import APIRouter
from rq import Worker

from app.core.redis import external_download_queue, redis_conn, retry_queue, tg_download_queue
from config.settings import settings

router = APIRouter(tags=["status"])


def _config_check() -> dict:
    """Check whether required Telegram credentials are configured."""
    missing = []
    if not settings.tg_api_id:
        missing.append({"field": "tg_api_id", "label": "Telegram API ID"})
    if not settings.tg_api_hash:
        missing.append({"field": "tg_api_hash", "label": "Telegram API Hash"})
    return {"valid": len(missing) == 0, "missing": missing}


def _tg_session_exists() -> bool:
    """Return True if a Pyrogram session file exists on disk."""
    session_file = settings.session_dir / f"{settings.tg_session_name}.session"
    return session_file.exists()


@router.get("/status")
async def get_status():
    """Return service health: config validity, TG session, queue depths, workers."""
    cfg = _config_check()
    session_exists = _tg_session_exists()

    # Queue depths (synchronous Redis call — very fast)
    try:
        queues = {
            "tg_pending": len(tg_download_queue),
            "external_pending": len(external_download_queue),
            "retry_pending": len(retry_queue),
            "ok": True,
        }
    except Exception:
        queues = {"ok": False, "tg_pending": 0, "external_pending": 0, "retry_pending": 0}

    # RQ workers currently registered with Redis
    try:
        workers = Worker.all(connection=redis_conn)
        worker_info = [
            {
                "name": w.name,
                "queues": [q.name for q in w.queues],
                "state": w.get_state(),
            }
            for w in workers
        ]
    except Exception:
        worker_info = []

    return {
        "config": cfg,
        "tg_session_exists": session_exists,
        "tg_ready": cfg["valid"] and session_exists,
        "queues": queues,
        "workers": worker_info,
    }
