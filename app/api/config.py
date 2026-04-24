"""Config management API: read / save .env through web UI."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

import httpx

from config.settings import CONFIG_DIR, ENV_FILE, reload_settings, settings

router = APIRouter(prefix="/config", tags=["config"])

# ---- schemas --------------------------------------------------------


class ConfigData(BaseModel):
    """Matches every editable field in Settings."""
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_name: str = "media_hub"
    tg_monitored_chats: str = ""
    tg_phone_number: str = ""
    database_url: str = ""
    redis_url: str = ""
    metube_url: str = ""
    storage_root: str = "/media"
    temp_dir: str = "/media/temp"
    tg_download_workers: int = 3
    external_download_workers: int = 5
    max_retries: int = 5
    retry_base_delay: int = 30
    proxy_pool: str = ""
    ytdlp_format: str = "bestvideo+bestaudio/best"
    ytdlp_use_aria2: bool = True
    tg_parallel_connections: int = 4
    tg_parallel_threshold: int = 10
    tg_notify_on_complete: bool = True
    tg_notify_on_fail: bool = True


# ---- helpers ---------------------------------------------------------

def _read_env() -> dict[str, str]:
    """Parse .env into a dict (simple KEY=VALUE, ignoring comments)."""
    data: dict[str, str] = {}
    if not ENV_FILE.exists():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def _write_env(data: dict[str, str]) -> None:
    """Write dict back to .env file."""
    lines: list[str] = []
    for k, v in data.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---- routes ----------------------------------------------------------

@router.get("/", response_model=ConfigData)
async def get_config():
    """Return current configuration values."""
    return ConfigData(
        tg_api_id=settings.tg_api_id,
        tg_api_hash=settings.tg_api_hash,
        tg_session_name=settings.tg_session_name,
        tg_monitored_chats=settings.tg_monitored_chats,
        tg_phone_number=settings.tg_phone_number,
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        metube_url=settings.metube_url,
        storage_root=settings.storage_root,
        temp_dir=settings.temp_dir,
        tg_download_workers=settings.tg_download_workers,
        external_download_workers=settings.external_download_workers,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        proxy_pool=settings.proxy_pool,
        ytdlp_format=settings.ytdlp_format,
        ytdlp_use_aria2=settings.ytdlp_use_aria2,
        tg_parallel_connections=settings.tg_parallel_connections,
        tg_parallel_threshold=settings.tg_parallel_threshold,
        tg_notify_on_complete=settings.tg_notify_on_complete,
        tg_notify_on_fail=settings.tg_notify_on_fail,
    )


@router.put("/")
async def save_config(body: ConfigData):
    """Save configuration to .env and reload settings."""
    env_map: dict[str, str] = {
        "TG_API_ID": str(body.tg_api_id),
        "TG_API_HASH": body.tg_api_hash,
        "TG_SESSION_NAME": body.tg_session_name,
        "TG_MONITORED_CHATS": body.tg_monitored_chats,
        "TG_PHONE_NUMBER": body.tg_phone_number,
        "DATABASE_URL": body.database_url,
        "REDIS_URL": body.redis_url,
        "METUBE_URL": body.metube_url,
        "STORAGE_ROOT": body.storage_root,
        "TEMP_DIR": body.temp_dir,
        "TG_DOWNLOAD_WORKERS": str(body.tg_download_workers),
        "EXTERNAL_DOWNLOAD_WORKERS": str(body.external_download_workers),
        "MAX_RETRIES": str(body.max_retries),
        "RETRY_BASE_DELAY": str(body.retry_base_delay),
        "PROXY_POOL": body.proxy_pool,
        "YTDLP_FORMAT": body.ytdlp_format,
        "YTDLP_USE_ARIA2": str(body.ytdlp_use_aria2).lower(),
        "TG_PARALLEL_CONNECTIONS": str(body.tg_parallel_connections),
        "TG_PARALLEL_THRESHOLD": str(body.tg_parallel_threshold),
        "TG_NOTIFY_ON_COMPLETE": str(body.tg_notify_on_complete).lower(),
        "TG_NOTIFY_ON_FAIL": str(body.tg_notify_on_fail).lower(),
    }

    _write_env(env_map)
    new_settings = reload_settings()
    logger.info("Configuration saved and reloaded.")
    return {"message": "Configuration saved", "env_path": str(ENV_FILE)}


# ---- proxy test ------------------------------------------------------

TEST_URL = "https://www.google.com/generate_204"
TEST_TIMEOUT = 10  # seconds


class ProxyTestReq(BaseModel):
    proxies: list[str]


class ProxyTestResult(BaseModel):
    proxy: str
    ok: bool
    latency_ms: int | None = None
    error: str | None = None


async def _test_one_proxy(proxy_url: str) -> ProxyTestResult:
    """Test a single proxy by fetching a lightweight URL."""
    proxy_url = proxy_url.strip()
    if not proxy_url:
        return ProxyTestResult(proxy=proxy_url, ok=False, error="Empty URL")

    try:
        start = time.monotonic()
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=TEST_TIMEOUT,
            verify=False,
        ) as client:
            resp = await client.get(TEST_URL)
        elapsed = int((time.monotonic() - start) * 1000)

        if resp.status_code < 400:
            return ProxyTestResult(proxy=proxy_url, ok=True, latency_ms=elapsed)
        return ProxyTestResult(
            proxy=proxy_url, ok=False, latency_ms=elapsed,
            error=f"HTTP {resp.status_code}",
        )
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return ProxyTestResult(
            proxy=proxy_url, ok=False, latency_ms=elapsed,
            error=str(e)[:200],
        )


@router.post("/proxy-test", response_model=list[ProxyTestResult])
async def test_proxies(body: ProxyTestReq):
    """Test one or more proxy URLs concurrently and return results."""
    if not body.proxies:
        raise HTTPException(400, "No proxies provided")
    if len(body.proxies) > 20:
        raise HTTPException(400, "Max 20 proxies per test")

    tasks = [_test_one_proxy(p) for p in body.proxies]
    results = await asyncio.gather(*tasks)
    return list(results)
