"""Agent catalog and company-scoped agent management API."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.agents import AgentRegistry
from app.api.deps import company_scope, get_agent_service, get_principal, require_company_admin
from app.schemas.agent import (
    AgentCatalogRead,
    AgentDefinitionRead,
    AgentStatusUpdate,
    CompanyAgentRead,
)
from app.security.authorization import CompanyScope
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


def _company_agent_read(agent, registry: AgentRegistry) -> CompanyAgentRead:
    definition = registry.get(agent.agent_key)
    return CompanyAgentRead(
        id=agent.id,
        company_id=agent.company_id,
        agent_key=agent.agent_key,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        definition=AgentDefinitionRead(
            key=definition.key,
            name=definition.name,
            description=definition.description,
            capabilities=list(definition.capabilities),
            maturity=definition.maturity,
            dispatch_ready=definition.dispatch_ready,
        ),
    )


@router.get("/catalog", response_model=AgentCatalogRead)
async def list_agent_catalog(
    request: Request,
    _principal: Annotated[object, Depends(get_principal)],
) -> AgentCatalogRead:
    registry: AgentRegistry = request.app.state.agent_registry
    return AgentCatalogRead(
        items=[
            AgentDefinitionRead(
                key=item.key,
                name=item.name,
                description=item.description,
                capabilities=list(item.capabilities),
                maturity=item.maturity,
                dispatch_ready=item.dispatch_ready,
            )
            for item in registry.list_definitions()
        ]
    )


@router.get("", response_model=list[CompanyAgentRead])
async def list_company_agents(
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentService, Depends(get_agent_service)],
) -> list[CompanyAgentRead]:
    rows = await service.list_company_agents(scope.company_id)
    return [_company_agent_read(row, service.registry) for row in rows]


@router.put("/{agent_key}", response_model=CompanyAgentRead, status_code=status.HTTP_200_OK)
async def provision_company_agent(
    agent_key: str,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    _admin: Annotated[object, Depends(require_company_admin)],
) -> CompanyAgentRead:
    agent = await service.provision(scope.company_id, agent_key)
    return _company_agent_read(agent, service.registry)


@router.patch("/{agent_key}/status", response_model=CompanyAgentRead)
async def update_company_agent_status(
    agent_key: str,
    payload: AgentStatusUpdate,
    scope: Annotated[CompanyScope, Depends(company_scope)],
    service: Annotated[AgentService, Depends(get_agent_service)],
    _admin: Annotated[object, Depends(require_company_admin)],
) -> CompanyAgentRead:
    agent = await service.set_status(scope.company_id, agent_key, payload.status)
    return _company_agent_read(agent, service.registry)
