"""Readiness probing for the API's runtime dependencies."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.redis import RedisConnectionManager, RedisHealth

logger = logging.getLogger("app.readiness")


@dataclass(frozen=True)
class DependencyCheck:
    status: str  # "ok" | "unavailable"
    latency_ms: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    status: str  # "ready" | "degraded"
    checks: dict[str, DependencyCheck] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"


class ReadinessService:
    """Checks database and Redis connectivity with per-check latency."""

    def __init__(self, engine: AsyncEngine, redis_manager: RedisConnectionManager) -> None:
        self._engine = engine
        self._redis = redis_manager

    async def check(self, *, timeout: float = 3.0) -> ReadinessReport:
        database, redis_health = await asyncio.gather(
            self._check_database(timeout=timeout),
            self._check_redis(timeout=timeout),
        )
        checks = {"database": database, "redis": redis_health}
        status = "ready" if all(c.status == "ok" for c in checks.values()) else "degraded"
        return ReadinessReport(status=status, checks=checks)

    async def _check_database(self, *, timeout: float) -> DependencyCheck:
        started = time.perf_counter()

        async def _ping() -> None:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

        try:
            await asyncio.wait_for(_ping(), timeout=timeout)
        except TimeoutError:
            return DependencyCheck(status="unavailable", error=f"database check timed out after {timeout}s")
        except OSError as exc:
            # asyncpg connection failures surface as OSError subclasses; keep
            # messages out of logs that could embed DSN details.
            logger.warning("database readiness check failed: %s", type(exc).__name__)
            return DependencyCheck(
                status="unavailable", latency_ms=None, error=f"{type(exc).__name__}: {exc}"
            )
        except Exception as exc:  # noqa: BLE001 - readiness must never raise
            logger.warning("database readiness check failed: %s", type(exc).__name__)
            return DependencyCheck(
                status="unavailable", latency_ms=None, error=f"{type(exc).__name__}: {exc}"
            )
        return DependencyCheck(status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))

    async def _check_redis(self, *, timeout: float) -> DependencyCheck:
        health: RedisHealth = await self._redis.health_check(timeout=timeout)
        return DependencyCheck(status=health.status, latency_ms=health.latency_ms, error=health.error)
