"""Proxy pool management backed by the database.

Proxies are seeded from settings.proxy_list at startup. The pool tracks
latency and failure counts so that workers always get the best available
proxy. A background health-check loop in the app process keeps statuses
current and re-enables recovered proxies automatically.

Failure model
-------------
- Each failed download (task failure) increments ``fail_count``.
- Each successful periodic health check resets ``fail_count`` to 0 and
  marks the proxy ACTIVE again.
- Once ``fail_count >= settings.proxy_fail_threshold``, the proxy is
  marked FAILED and skipped by workers until the health checker recovers it.
- Proxies with status DISABLED are never touched automatically.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
from loguru import logger
from sqlalchemy import select, update

from app.core.database import async_session_factory
from app.models.task import Proxy, ProxyStatus
from app.core.settings import settings

_HEALTH_CHECK_URL = "https://www.google.com/generate_204"
_HEALTH_CHECK_TIMEOUT = 10  # seconds


class ProxyPool:
    """Async, database-backed proxy pool."""

    # ------------------------------------------------------------------ #
    # Startup seeding                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def sync_from_settings() -> int:
        """Upsert proxies in settings.proxy_list into the database.

        Already-known proxies (any status) are left untouched so that their
        health state is preserved across restarts.

        Returns the number of newly added proxies.
        """
        proxy_urls = settings.proxy_list
        if not proxy_urls:
            return 0

        added = 0
        async with async_session_factory() as session:
            for url in proxy_urls:
                result = await session.execute(
                    select(Proxy).where(Proxy.proxy_url == url)
                )
                if result.scalar_one_or_none() is None:
                    session.add(
                        Proxy(proxy_url=url, status=ProxyStatus.ACTIVE, fail_count=0)
                    )
                    added += 1
            if added:
                await session.commit()

        if added:
            logger.info(f"ProxyPool: seeded {added} new proxy/proxies from settings.")
        return added

    # ------------------------------------------------------------------ #
    # Worker interface                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_best_proxy() -> str | None:
        """Return the active proxy URL with the lowest latency.

        Proxies with unknown latency are placed last. Returns *None* when no
        active proxies are available (workers proceed without a proxy).
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Proxy)
                .where(Proxy.status == ProxyStatus.ACTIVE)
                .order_by(Proxy.latency.nulls_last(), Proxy.fail_count)
                .limit(1)
            )
            proxy = result.scalar_one_or_none()
            return proxy.proxy_url if proxy else None

    @staticmethod
    async def report_success(proxy_url: str) -> None:
        """Record a successful use; resets the failure counter."""
        async with async_session_factory() as session:
            await session.execute(
                update(Proxy)
                .where(Proxy.proxy_url == proxy_url)
                .values(
                    status=ProxyStatus.ACTIVE,
                    fail_count=0,
                    last_check_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    @staticmethod
    async def report_failure(proxy_url: str) -> None:
        """Increment failure counter; auto-disable after threshold is reached."""
        threshold = settings.proxy_fail_threshold
        async with async_session_factory() as session:
            result = await session.execute(
                select(Proxy).where(Proxy.proxy_url == proxy_url)
            )
            proxy = result.scalar_one_or_none()
            if proxy is None:
                return

            proxy.fail_count = (proxy.fail_count or 0) + 1
            proxy.last_check_at = datetime.now(timezone.utc)

            if proxy.fail_count >= threshold:
                proxy.status = ProxyStatus.FAILED
                logger.warning(
                    f"ProxyPool: {proxy_url!r} marked FAILED after "
                    f"{proxy.fail_count} consecutive failures."
                )

            await session.commit()

    # ------------------------------------------------------------------ #
    # Health checker                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def check_all() -> dict[str, bool]:
        """Test every non-DISABLED proxy and update its status and latency.

        - On success: status → ACTIVE, fail_count reset to 0, latency updated.
        - On failure: fail_count incremented; status → FAILED when threshold met.

        Returns a mapping of {proxy_url: ok}.
        """
        async with async_session_factory() as session:
            result = await session.execute(
                select(Proxy).where(Proxy.status != ProxyStatus.DISABLED)
            )
            # Snapshot (id, url, current fail_count) to avoid keeping session open
            rows = [
                (p.id, p.proxy_url, p.fail_count or 0)
                for p in result.scalars().all()
            ]

        if not rows:
            logger.debug("ProxyPool: no proxies to check.")
            return {}

        threshold = settings.proxy_fail_threshold
        results: dict[str, bool] = {}

        for proxy_id, proxy_url, fail_count in rows:
            ok, latency_ms = await ProxyPool._test_one(proxy_url)
            results[proxy_url] = ok

            if ok:
                new_status = ProxyStatus.ACTIVE
                new_fail_count = 0
                update_values: dict = dict(
                    status=new_status,
                    fail_count=new_fail_count,
                    latency=round(latency_ms, 1),
                    last_check_at=datetime.now(timezone.utc),
                )
                tag = f"✓ {latency_ms:.0f} ms"
            else:
                new_fail_count = fail_count + 1
                new_status = (
                    ProxyStatus.FAILED
                    if new_fail_count >= threshold
                    else ProxyStatus.ACTIVE
                )
                update_values = dict(
                    status=new_status,
                    fail_count=new_fail_count,
                    last_check_at=datetime.now(timezone.utc),
                )
                tag = f"✗  fails={new_fail_count}/{threshold}"
                if new_status == ProxyStatus.FAILED:
                    logger.warning(f"ProxyPool: {proxy_url!r} marked FAILED by health check.")

            async with async_session_factory() as session:
                await session.execute(
                    update(Proxy).where(Proxy.id == proxy_id).values(**update_values)
                )
                await session.commit()

            logger.info(f"ProxyPool check {proxy_url!r}: {tag}")

        ok_count = sum(1 for v in results.values() if v)
        logger.info(
            f"ProxyPool health check done: {ok_count}/{len(results)} proxies healthy."
        )
        return results

    @staticmethod
    async def _test_one(proxy_url: str) -> tuple[bool, float]:
        """Probe a single proxy. Returns (success, latency_ms)."""
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=_HEALTH_CHECK_TIMEOUT,
                verify=True,
            ) as client:
                resp = await client.get(_HEALTH_CHECK_URL)
            latency_ms = (time.monotonic() - start) * 1000
            return resp.status_code < 400, latency_ms
        except Exception:
            latency_ms = (time.monotonic() - start) * 1000
            return False, latency_ms
