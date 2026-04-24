"""Proxy pool REST API – CRUD and manual health-check trigger."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.core.database import async_session_factory
from app.models.task import Proxy, ProxyStatus
from app.services.proxy_pool import ProxyPool

router = APIRouter(prefix="/proxies", tags=["proxies"])


# ---- schemas --------------------------------------------------------


class ProxyCreate(BaseModel):
    proxy_url: str


class ProxyResponse(BaseModel):
    id: int
    proxy_url: str
    status: str
    latency: float | None = None
    fail_count: int = 0
    last_check_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- routes ---------------------------------------------------------


@router.get("/", response_model=list[ProxyResponse])
async def list_proxies():
    """Return all proxies with current status, latency and failure counts."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Proxy).order_by(Proxy.status, Proxy.latency.nulls_last())
        )
        return [ProxyResponse.model_validate(p) for p in result.scalars().all()]


@router.post("/", response_model=ProxyResponse, status_code=201)
async def add_proxy(body: ProxyCreate):
    """Add a new proxy to the pool."""
    url = body.proxy_url.strip()
    if not url:
        raise HTTPException(400, "proxy_url is required")

    async with async_session_factory() as session:
        existing = await session.execute(
            select(Proxy).where(Proxy.proxy_url == url)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(409, "Proxy already exists")

        proxy = Proxy(proxy_url=url, status=ProxyStatus.ACTIVE, fail_count=0)
        session.add(proxy)
        await session.commit()
        await session.refresh(proxy)
        return ProxyResponse.model_validate(proxy)


@router.delete("/{proxy_id}", status_code=204)
async def delete_proxy(proxy_id: int):
    """Remove a proxy from the pool."""
    async with async_session_factory() as session:
        proxy = await session.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(404, "Proxy not found")
        await session.delete(proxy)
        await session.commit()


@router.post("/{proxy_id}/enable", response_model=ProxyResponse)
async def enable_proxy(proxy_id: int):
    """Re-enable a FAILED proxy; resets its failure counter."""
    async with async_session_factory() as session:
        proxy = await session.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(404, "Proxy not found")
        proxy.status = ProxyStatus.ACTIVE
        proxy.fail_count = 0
        await session.commit()
        await session.refresh(proxy)
        return ProxyResponse.model_validate(proxy)


@router.post("/{proxy_id}/disable", response_model=ProxyResponse)
async def disable_proxy(proxy_id: int):
    """Permanently disable a proxy (health checker will not touch it)."""
    async with async_session_factory() as session:
        proxy = await session.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(404, "Proxy not found")
        proxy.status = ProxyStatus.DISABLED
        await session.commit()
        await session.refresh(proxy)
        return ProxyResponse.model_validate(proxy)


@router.post("/check")
async def trigger_health_check():
    """Manually trigger a health check for all non-disabled proxies."""
    results = await ProxyPool.check_all()
    ok_count = sum(1 for v in results.values() if v)
    return {
        "checked": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": {url: ("ok" if ok else "failed") for url, ok in results.items()},
    }


@router.post("/sync")
async def sync_from_settings():
    """Sync proxies from the settings proxy_pool string into the database."""
    added = await ProxyPool.sync_from_settings()
    return {"added": added, "message": f"Added {added} new proxy/proxies from settings."}
