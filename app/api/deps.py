"""FastAPI dependencies.

Runtime objects (engine, session factory, redis manager, provider registry,
master key box, principal) live on ``app.state`` and are attached by the
lifespan; tests can attach their own instances.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisConnectionManager
from app.integrations.providers import ProviderExecutor, ProviderRegistry, RetryPolicy
from app.security.authorization import (
    CompanyScope,
    Principal,
    authorize_company_access,
)
from app.security.crypto import MasterKeyBox
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
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


async def get_connection_service(request: Request) -> AsyncIterator[ConnectionService]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield ConnectionService(
                session,
                request.app.state.provider_registry,
                master_key_box=request.app.state.master_key_box,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_connection_validation_service(
    request: Request,
) -> AsyncIterator[ConnectionValidationService]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield ConnectionValidationService(
                session,
                request.app.state.provider_registry,
                request.app.state.provider_executor,
                request.app.state.master_key_box,
            )
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


def get_provider_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


def get_provider_executor(request: Request) -> ProviderExecutor:
    return request.app.state.provider_executor


def get_master_key_box(request: Request) -> MasterKeyBox:
    return request.app.state.master_key_box


def get_principal(request: Request) -> Principal:
    return request.app.state.principal


async def company_scope(request: Request) -> CompanyScope:
    """Resolve and authorize the acting company for the request.

    The company context arrives via the ``X-Company-ID`` header. Requests
    without it resolve to the default company. Authorization is enforced by
    ``authorize_company_access`` — the single decision point. The acting
    principal always comes from ``app.state`` (never from request parameters,
    so it can never be spoofed or accidentally treated as a request body).
    """
    acting_principal: Principal = request.app.state.principal
    raw_company_id = request.headers.get("X-Company-ID")

    if raw_company_id:
        try:
            company_id = uuid.UUID(raw_company_id)
        except ValueError as exc:
            from app.core.exceptions import UnprocessableEntityError

            raise UnprocessableEntityError(
                "X-Company-ID must be a UUID.",
                details=[{"field": "X-Company-ID", "value": raw_company_id}],
            ) from exc
    else:
        company_id = await _resolve_default_company_id(request)

    return authorize_company_access(acting_principal, company_id)


async def _resolve_default_company_id(request: Request) -> uuid.UUID:
    """Return the id of the shared ``default`` company, creating it once."""
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.models import Company

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        result = await session.execute(select(Company).where(Company.slug == "default"))
        company = result.scalar_one_or_none()
        if company is not None:
            return company.id
        company = Company(name="Default Company", slug="default")
        session.add(company)
        try:
            await session.commit()
        except IntegrityError:
            # Another request created it concurrently.
            await session.rollback()
            result = await session.execute(select(Company).where(Company.slug == "default"))
            company = result.scalar_one()
        return company.id


def pagination_params(
    limit: int = Query(default=20, ge=1, le=100, description="Maximum items to return"),
    offset: int = Query(default=0, ge=0, description="Number of items to skip"),
) -> tuple[int, int]:
    return limit, offset


def build_provider_executor(settings) -> ProviderExecutor:  # noqa: ANN001 - Settings
    return ProviderExecutor(
        default_policy=RetryPolicy(
            max_attempts=settings.provider_max_attempts,
            backoff_base_seconds=settings.provider_retry_backoff_seconds,
        ),
        default_timeout_seconds=settings.provider_default_timeout_seconds,
    )
