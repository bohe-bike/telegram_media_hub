"""Config management API: read / save config.toml through web UI."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import tomlkit
from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

import httpx

from config.settings import CONFIG_DIR, TOML_FILE, reload_settings, settings

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
    tg_parallel_connections: int = 4
    tg_parallel_threshold: int = 10
    tg_notify_on_complete: bool = True
    tg_notify_on_fail: bool = True
    api_secret_key: str = ""
    proxy_fail_threshold: int = 3
    proxy_check_interval: int = 300


# ---- helpers ---------------------------------------------------------

def _read_toml() -> dict:
    """Parse config.toml into a flat dict (merges all top-level sections)."""
    if not TOML_FILE.exists():
        return {}
    with open(TOML_FILE, "rb") as f:
        import tomllib
        data = tomllib.load(f)
    flat: dict = {}
    for key, val in data.items():
        if isinstance(val, dict):
            flat.update(val)
        else:
            flat[key] = val
    return flat


_SECTION_ORDER: list[tuple[str, list[str]]] = [
    ("Telegram", [
        "tg_api_id", "tg_api_hash", "tg_session_name",
        "tg_phone_number", "tg_monitored_chats",
    ]),
    ("Infrastructure", ["database_url", "redis_url", "metube_url"]),
    ("Storage", ["storage_root", "temp_dir"]),
    ("Workers & Retry", [
        "tg_download_workers", "external_download_workers",
        "max_retries", "retry_base_delay",
    ]),
    ("Proxy", ["proxy_pool", "proxy_fail_threshold", "proxy_check_interval"]),
    ("TG 并行下载", [
        "tg_parallel_connections", "tg_parallel_threshold",
    ]),
    ("Notifications", ["tg_notify_on_complete", "tg_notify_on_fail"]),
    ("Security", ["api_secret_key"]),
]


def _write_toml(data: dict) -> None:
    """Write config dict back to config.toml preserving section comments."""
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Telegram Media Hub – Configuration"))
    doc.add(tomlkit.comment("Edit values here or via the Web UI (Settings tab)."))
    doc.add(tomlkit.nl())

    written: set[str] = set()
    for section_name, keys in _SECTION_ORDER:
        doc.add(tomlkit.comment(f"{'=' * 60}"))
        doc.add(tomlkit.comment(f" {section_name}"))
        doc.add(tomlkit.comment(f"{'=' * 60}"))
        for key in keys:
            if key in data:
                doc.add(key, data[key])
                written.add(key)
        doc.add(tomlkit.nl())

    # Any keys not covered by the section order (future fields)
    extras = {k: v for k, v in data.items() if k not in written}
    if extras:
        doc.add(tomlkit.comment("Additional settings"))
        for key, val in extras.items():
            doc.add(key, val)

    TOML_FILE.write_text(tomlkit.dumps(doc), encoding="utf-8")


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
        tg_parallel_connections=settings.tg_parallel_connections,
        tg_parallel_threshold=settings.tg_parallel_threshold,
        tg_notify_on_complete=settings.tg_notify_on_complete,
        tg_notify_on_fail=settings.tg_notify_on_fail,
        api_secret_key=settings.api_secret_key,
        proxy_fail_threshold=settings.proxy_fail_threshold,
        proxy_check_interval=settings.proxy_check_interval,
    )


@router.put("/")
async def save_config(body: ConfigData):
    """Save configuration to config.toml and reload settings."""
    toml_map: dict = {
        "tg_api_id": body.tg_api_id,
        "tg_api_hash": body.tg_api_hash,
        "tg_session_name": body.tg_session_name,
        "tg_monitored_chats": body.tg_monitored_chats,
        "tg_phone_number": body.tg_phone_number,
        "database_url": body.database_url,
        "redis_url": body.redis_url,
        "metube_url": body.metube_url,
        "storage_root": body.storage_root,
        "temp_dir": body.temp_dir,
        "tg_download_workers": body.tg_download_workers,
        "external_download_workers": body.external_download_workers,
        "max_retries": body.max_retries,
        "retry_base_delay": body.retry_base_delay,
        "proxy_pool": body.proxy_pool,
        "proxy_fail_threshold": body.proxy_fail_threshold,
        "proxy_check_interval": body.proxy_check_interval,
        "tg_parallel_connections": body.tg_parallel_connections,
        "tg_parallel_threshold": body.tg_parallel_threshold,
        "tg_notify_on_complete": body.tg_notify_on_complete,
        "tg_notify_on_fail": body.tg_notify_on_fail,
        "api_secret_key": body.api_secret_key,
    }

    _write_toml(toml_map)
    reload_settings()
    logger.info("Configuration saved to config.toml and reloaded.")
    return {"message": "Configuration saved", "toml_path": str(TOML_FILE)}


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

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(
            proxy=proxy_url,
            timeout=TEST_TIMEOUT,
            verify=True,
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
