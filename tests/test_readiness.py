"""Readiness endpoint: distinguishes liveness from dependency readiness."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.redis import RedisConnectionManager
from app.main import create_app
from tests.conftest import DEAD_DATABASE_URL


async def test_readiness_ready_when_dependencies_up(client):
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"database", "redis"}
    for check in body["checks"].values():
        assert check["status"] == "ok"
        assert check["latency_ms"] is not None
        assert check["error"] is None


async def test_readiness_degraded_when_redis_down(clean_database, engine, session_factory):
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis_manager = RedisConnectionManager("redis://localhost:6390/0")  # nothing listens here

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "unavailable"
    assert body["checks"]["redis"]["error"]


async def test_readiness_degraded_when_database_down(clean_database, session_factory, redis_manager):
    app = create_app()
    app.state.session_factory = session_factory
    app.state.redis_manager = redis_manager
    app.state.engine = create_async_engine(
        DEAD_DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 2}
    )

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            response = await client.get("/api/v1/health/ready")
        finally:
            await app.state.engine.dispose()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"]["status"] == "unavailable"
    assert body["checks"]["redis"]["status"] == "ok"


async def test_liveness_succeeds_even_with_dependencies_down(clean_database, engine, session_factory):
    """Liveness must not probe dependencies — that is readiness's job."""
    app = create_app()
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis_manager = RedisConnectionManager("redis://localhost:6390/0")

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
