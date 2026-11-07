"""Async engine and session factory construction.

Engines are created by the application lifespan (or explicitly by tests) and
stored on ``app.state``; there is no import-time global connection state.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async SQLAlchemy engine from settings."""
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory.

    ``expire_on_commit=False`` keeps ORM objects readable after the unit-of-work
    commit performed by the session dependency.
    """
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
