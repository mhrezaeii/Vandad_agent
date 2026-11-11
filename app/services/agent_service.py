"""Tenant-scoped provisioning and lifecycle operations for company agents."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents import AgentRegistry, UnknownAgentError
from app.core.exceptions import ConflictError, NotFoundError
from app.models import CompanyAgent
from app.services.audit_service import AuditService


class AgentService:
    def __init__(self, session: AsyncSession, registry: AgentRegistry) -> None:
        self.session = session
        self.registry = registry
        self.audit = AuditService(session)

    async def list_company_agents(self, company_id: uuid.UUID) -> list[CompanyAgent]:
        result = await self.session.scalars(
            select(CompanyAgent)
            .where(CompanyAgent.company_id == company_id)
            .order_by(CompanyAgent.agent_key.asc())
        )
        return list(result.all())

    async def provision(self, company_id: uuid.UUID, agent_key: str) -> CompanyAgent:
        try:
            definition = self.registry.get(agent_key)
        except UnknownAgentError as exc:
            raise NotFoundError(
                "Agent definition not found.",
                details=[{"field": "agent_key", "value": agent_key}],
            ) from exc

        existing = await self.session.scalar(
            select(CompanyAgent).where(
                CompanyAgent.company_id == company_id,
                CompanyAgent.agent_key == definition.key,
            )
        )
        if existing is not None:
            return existing

        agent = CompanyAgent(
            company_id=company_id,
            agent_key=definition.key,
            status="disabled",
        )
        self.session.add(agent)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # A concurrent idempotent provision may have won the unique race.
            await self.session.rollback()
            existing = await self.session.scalar(
                select(CompanyAgent).where(
                    CompanyAgent.company_id == company_id,
                    CompanyAgent.agent_key == definition.key,
                )
            )
            if existing is not None:
                return existing
            raise ConflictError("Agent provisioning conflicted with another operation.") from exc

        self.audit.record(
            event_type="agent.provisioned",
            action="provision",
            company_id=company_id,
            resource_type="company_agent",
            resource_id=agent.id,
            metadata={"agent_key": agent.agent_key},
        )
        return agent

    async def set_status(
        self, company_id: uuid.UUID, agent_key: str, status: str
    ) -> CompanyAgent:
        try:
            self.registry.get(agent_key)
        except UnknownAgentError as exc:
            raise NotFoundError(
                "Agent definition not found.",
                details=[{"field": "agent_key", "value": agent_key}],
            ) from exc

        agent = await self.session.scalar(
            select(CompanyAgent).where(
                CompanyAgent.company_id == company_id,
                CompanyAgent.agent_key == agent_key,
            )
        )
        if agent is None:
            raise NotFoundError(
                "Agent has not been provisioned for this company.",
                details=[{"field": "agent_key", "value": agent_key}],
            )
        if agent.status != status:
            previous_status = agent.status
            agent.status = status
            await self.session.flush()
            # TimestampMixin uses a SQL expression for onupdate; refresh before
            # response serialization so async ORM access never triggers implicit IO.
            await self.session.refresh(agent)
            self.audit.record(
                event_type="agent.status_changed",
                action="change_status",
                company_id=company_id,
                resource_type="company_agent",
                resource_id=agent.id,
                metadata={
                    "agent_key": agent.agent_key,
                    "previous_status": previous_status,
                    "status": status,
                },
            )
        return agent
