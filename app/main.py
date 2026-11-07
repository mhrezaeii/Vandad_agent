"""Application entrypoint: app factory, lifespan, and ASGI instance."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.core.config import Settings, get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import CorrelationIdMiddleware
from app.core.redis import RedisConnectionManager
from app.database.session import create_engine, create_session_factory

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Attach runtime infrastructure to app.state; dispose on shutdown.

    Startup never aborts on a dependency failure: the readiness endpoint is
    the authority on dependency state, and containers orchestrate retries.
    """
    settings = get_settings()

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    redis_manager = RedisConnectionManager(
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        max_connections=settings.redis_max_connections,
    )

    try:
        async with engine.connect():
            logger.info("database connection verified")
    except Exception as exc:  # noqa: BLE001 - degraded start is a runtime decision
        logger.error("database connection check failed: %s", type(exc).__name__)

    try:
        await redis_manager.initialize()
    except Exception as exc:  # noqa: BLE001
        logger.error("redis initialization failed: %s", type(exc).__name__)

    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.redis_manager = redis_manager
    app.state.settings = settings

    yield

    await redis_manager.close()
    await engine.dispose()
    logger.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    resolved = settings or get_settings()
    setup_logging(resolved)

    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        lifespan=lifespan,
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None if resolved.is_production else "/redoc",
        openapi_url=None if resolved.is_production else "/openapi.json",
    )
    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=resolved.api_prefix)
    return app


app = create_app()
