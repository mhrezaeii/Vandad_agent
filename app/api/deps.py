"""FastAPI dependencies.

Runtime objects (engine, session factory, redis manager, provider registry,
master key box, principal) live on ``app.state`` and are attached by the
lifespan; tests can attach their own instances.
"""

from __future__ import annotations

import uuid
import hmac
from collections.abc import AsyncIterator

from fastapi import Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.queue import WorkflowQueue
from app.core.redis import RedisConnectionManager
from app.integrations.providers import ProviderExecutor, ProviderRegistry, RetryPolicy
from app.security.authorization import (
    CompanyScope,
    Principal,
    authorize_company_access,
)
from app.security.crypto import MasterKeyBox
from app.services.agent_service import AgentService
from app.services.agent_task_service import AgentTaskService
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
from app.services.platform_service import PlatformService
from app.services.readiness_service import ReadinessService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.services.workflow_service import WorkflowService


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


async def get_agent_service(request: Request) -> AsyncIterator[AgentService]:
    """Provide an agent service using the app's approved catalog."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield AgentService(session, request.app.state.agent_registry)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_agent_task_service(request: Request) -> AsyncIterator[AgentTaskService]:
    """Provide a task service bound to the company agent catalog."""
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield AgentTaskService(session, request.app.state.agent_registry)
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
    """Resolve the dev bootstrap identity or a verified production proxy identity."""
    settings = request.app.state.settings
    if not settings.is_production:
        return request.app.state.principal

    configured_secret = settings.auth_proxy_shared_secret
    supplied_secret = request.headers.get("X-Auth-Proxy-Secret", "")
    if configured_secret is None or not hmac.compare_digest(
        supplied_secret, configured_secret.get_secret_value()
    ):
        raise UnauthorizedError()

    subject = request.headers.get(settings.auth_proxy_user_header, "").strip()
    raw_companies = request.headers.get(settings.auth_proxy_company_ids_header, "")
    raw_roles = request.headers.get(settings.auth_proxy_roles_header, "")
    if not subject or len(subject) > 255 or not raw_companies:
        raise UnauthorizedError("The identity proxy did not provide a valid user and company scope.")

    try:
        company_ids = frozenset(str(uuid.UUID(item.strip())) for item in raw_companies.split(",") if item.strip())
    except ValueError as exc:
        raise UnauthorizedError("The identity proxy provided an invalid company claim.") from exc
    if not company_ids:
        raise UnauthorizedError("The identity proxy did not provide a company membership.")

    allowed_roles = {"owner", "admin", "operator", "viewer"}
    roles = frozenset(
        role.strip().lower()
        for role in raw_roles.split(",")
        if role.strip().lower() in allowed_roles
    )
    if not roles:
        raise ForbiddenError("The authenticated user has no recognized platform role.")
    return Principal(subject=subject, company_ids=company_ids, roles=roles)


def require_company_admin(request: Request) -> Principal:
    principal = get_principal(request)
    if not principal.roles.intersection({"owner", "admin"}):
        raise ForbiddenError("Company owner or administrator role is required.")
    return principal


def get_workflow_queue(request: Request) -> WorkflowQueue:
    queue = getattr(request.app.state, "workflow_queue", None)
    if queue is None:
        from app.core.exceptions import DependencyUnavailableError

        raise DependencyUnavailableError("The workflow queue is not available on this instance.")
    return queue


async def get_workflow_service(request: Request) -> AsyncIterator[WorkflowService]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield WorkflowService(session, request.app.state.provider_registry)
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_workflow_execution_service(request: Request) -> AsyncIterator[WorkflowExecutionService]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        try:
            yield WorkflowExecutionService(
                session,
                request.app.state.provider_registry,
                request.app.state.provider_executor,
                request.app.state.master_key_box,
                queue=getattr(request.app.state, "workflow_queue", None),
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def company_scope(request: Request) -> CompanyScope:
    """Resolve and authorize the acting company for the request.

    The company context arrives via the ``X-Company-ID`` header. Requests
    without it resolve to the default company. Authorization is enforced by
    ``authorize_company_access`` — the single decision point. The acting
    principal always comes from ``app.state`` (never from request parameters,
    so it can never be spoofed or accidentally treated as a request body).
    """
    acting_principal: Principal = get_principal(request)
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
        if request.app.state.settings.is_production:
            if len(acting_principal.company_ids) != 1:
                from app.core.exceptions import UnprocessableEntityError

                raise UnprocessableEntityError(
                    "X-Company-ID is required when the user belongs to multiple companies."
                )
            company_id = uuid.UUID(next(iter(acting_principal.company_ids)))
        else:
            company_id = await _resolve_default_company_id(request)

    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not acting_principal.roles.intersection(
        {"owner", "admin", "operator"}
    ):
        raise ForbiddenError("An operator role is required for this action.")

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
