"""Reusable async Redis connection manager.

Owns client lifecycle: initialization, health checking, and graceful
shutdown. Instances are created by the application lifespan (or tests) and
stored on ``app.state`` — there is no import-time global client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.client import Redis
from redis.exceptions import RedisError

logger = logging.getLogger("app.redis")


@dataclass(frozen=True)
class RedisHealth:
    status: str  # "ok" | "unavailable"
    latency_ms: float | None = None
    error: str | None = None


class RedisConnectionManager:
    """Initialize once, health-check often, close on shutdown."""

    def __init__(
        self,
        url: str,
        *,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
        max_connections: int = 10,
        health_check_interval: int = 30,
    ) -> None:
        self._url = url
        self._socket_timeout = socket_timeout
        self._socket_connect_timeout = socket_connect_timeout
        self._max_connections = max_connections
        self._health_check_interval = health_check_interval
        self._client: Redis | None = None

    @property
    def client(self) -> Redis | None:
        """The underlying client, or None before initialization."""
        return self._client

    @property
    def is_initialized(self) -> bool:
        return self._client is not None

    async def initialize(self) -> None:
        """Create the client and verify connectivity with a PING.

        Raises the underlying RedisError/ConnectionError when Redis is
        unreachable so callers (lifespan, tests) can decide how to react.
        """
        if self._client is not None:
            await self.close()

        client: Redis = aioredis.Redis.from_url(
            self._url,
            socket_timeout=self._socket_timeout,
            socket_connect_timeout=self._socket_connect_timeout,
            max_connections=self._max_connections,
            health_check_interval=self._health_check_interval,
            decode_responses=True,
        )
        await client.ping()  # fail fast on an unreachable server
        self._client = client
        logger.info("redis connection initialized")

    async def health_check(self, *, timeout: float | None = None) -> RedisHealth:
        """PING Redis and report status/latency; never raises."""
        if self._client is None:
            return RedisHealth(status="unavailable", error="redis client is not initialized")
        started = time.perf_counter()
        try:
            async def _ping() -> None:
                await self._client.ping()

            await asyncio.wait_for(_ping(), timeout=timeout)
        except TimeoutError:
            return RedisHealth(status="unavailable", error=f"redis ping timed out after {timeout}s")
        except (RedisError, OSError) as exc:
            logger.warning("redis health check failed: %s", type(exc).__name__)
            return RedisHealth(status="unavailable", error=f"{type(exc).__name__}: {exc}")
        return RedisHealth(status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))

    async def execute(self, command: str, *args: Any) -> Any:
        """Run a single command against the managed client."""
        if self._client is None:
            raise RuntimeError("redis client is not initialized; call initialize() first")
        return await self._client.execute_command(command, *args)

    async def close(self) -> None:
        """Release the connection pool; safe to call multiple times."""
        if self._client is None:
            return
        client, self._client = self._client, None
        try:
            await client.aclose()
            logger.info("redis connection closed")
        except (RedisError, OSError) as exc:  # shutdown must not fail on a dead socket
            logger.warning("error while closing redis connection: %s", type(exc).__name__)
