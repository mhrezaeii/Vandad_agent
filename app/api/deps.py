"""FastAPI dependencies.

Runtime objects (engine, session factory, redis manager) live on ``app.state``
and are attached by the lifespan; tests can attach their own instances.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisConnectionManager
from app.services.platform_service import PlatformService
from app.services.readiness_service import ReadinessService


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped unit of work: commit on success, rollback on any error."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_platform_service(request: Request) -> AsyncIterator[PlatformService]:
    """Provide a platform service bound to the request's unit of work."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield PlatformService(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_redis_manager(request: Request) -> RedisConnectionManager:
    return request.app.state.redis_manager


def get_readiness_service(request: Request) -> ReadinessService:
    return ReadinessService(
        engine=request.app.state.engine,
        redis_manager=request.app.state.redis_manager,
    )


def pagination_params(
    limit: int = Query(default=20, ge=1, le=100, description="Maximum items to return"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
) -> tuple[int, int]:
    return limit, offset
