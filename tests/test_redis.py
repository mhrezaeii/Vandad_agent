"""Redis connection manager behavior."""

from __future__ import annotations

import pytest
from redis.exceptions import RedisError

from app.core.redis import RedisConnectionManager


@pytest.fixture
def dead_url() -> str:
    return "redis://localhost:6390/0"  # nothing listens here in the test env


async def test_initialize_and_health_check(redis_manager):
    assert redis_manager.is_initialized
    health = await redis_manager.health_check()
    assert health.status == "ok"
    assert health.latency_ms is not None
    assert health.error is None


async def test_execute_roundtrip(redis_manager):
    assert await redis_manager.execute("SET", "test:roundtrip", "value-1") is True
    assert await redis_manager.execute("GET", "test:roundtrip") == "value-1"


async def test_execute_before_initialize_raises(dead_url):
    manager = RedisConnectionManager(dead_url)
    with pytest.raises(RuntimeError, match="not initialized"):
        await manager.execute("PING")


async def test_uninitialized_health_check_is_unavailable(dead_url):
    manager = RedisConnectionManager(dead_url)
    health = await manager.health_check()
    assert health.status == "unavailable"
    assert "not initialized" in health.error


async def test_health_check_reports_connection_failure(redis_manager, monkeypatch):
    """An initialized manager whose server dies reports unavailable, never raises."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    async def failing_ping() -> None:
        raise RedisConnectionError("connection closed by server")

    monkeypatch.setattr(redis_manager.client, "ping", failing_ping)
    health = await redis_manager.health_check(timeout=2.0)
    assert health.status == "unavailable"
    assert "connection closed" in health.error


async def test_initialize_failure_raises(dead_url):
    manager = RedisConnectionManager(dead_url, socket_connect_timeout=1.0, socket_timeout=1.0)
    with pytest.raises(RedisError):
        await manager.initialize()
    assert not manager.is_initialized


async def test_close_is_idempotent(redis_manager):
    await redis_manager.close()
    assert redis_manager.client is None
    await redis_manager.close()  # second close is a no-op
    assert not redis_manager.is_initialized
