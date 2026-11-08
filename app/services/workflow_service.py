"""Workflow management service: CRUD, versioning, lifecycle, validation.

Lifecycle: draft -> published (validation required) -> paused -> published /
archived. Editing a published workflow creates a new draft version; publishing
promotes that draft and makes it the workflow's current version. All operations
are tenant-checked and audited.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.correlation import get_correlation_id
from app.core.exceptions import ConflictError, NotFoundError
from app.integrations.providers import ProviderRegistry
from app.models import (
    Workflow,
    WorkflowDefinitionStatus,
    WorkflowEdge,
    WorkflowNode,
    WorkflowVersion,
    WorkflowVersionStatus,
)
from app.security.authorization import CompanyScope
from app.services.audit_service import AuditService
from app.workflows.validation import (
    GraphValidator,
    WorkflowValidationError,
    load_version_graph,
)

_WORKFLOW_RESOURCE = "workflow"

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"published", "archived"},
    "published": {"paused", "archived"},
    "paused": {"published", "archived"},
    "archived": set(),
}


class WorkflowService:
    def __init__(self, session: AsyncSession, registry: ProviderRegistry) -> None:
        self.session = session
        self.registry = registry
        self.audit = AuditService(session)

    # --- CRUD ------------------------------------------------------------------
    async def create_workflow(
        self,
        *,
        scope: CompanyScope,
        name: str,
        description: str | None,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        default_execution_settings: dict[str, Any] | None = None,
    ) -> Workflow:
        if await self._name_exists(scope.company_id, name):
            raise ConflictError(
                f"A workflow named '{name}' already exists for this company.",
                details=[{"field": "name", "value": name}],
            )
        workflow = Workflow(
            company_id=scope.company_id,
            name=name,
            description=description,
            status=WorkflowDefinitionStatus.DRAFT.value,
            default_execution_settings=default_execution_settings or {},
        )
        self.session.add(workflow)
        await self.session.flush()
        await self._write_draft_version(workflow, nodes=nodes, edges=edges)
        self.audit.record(
            event_type="workflow.created",
            action="create",
            company_id=scope.company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={"name": name},
        )
        await self.session.flush()
        return workflow

    async def get_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> Workflow:
        workflow = await self.session.get(Workflow, workflow_id)
        if workflow is None or workflow.company_id != company_id:
            raise NotFoundError(
                "Workflow not found.", details=[{"field": "workflow_id", "value": str(workflow_id)}]
            )
        return workflow

    async def list_workflows(
        self,
        *,
        company_id: uuid.UUID,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Workflow], int]:
        conditions = [Workflow.company_id == company_id]
        if status is not None:
            conditions.append(Workflow.status == status)
        total = int(
            (
                await self.session.execute(select(func.count()).select_from(Workflow).where(*conditions))
            ).scalar_one()
        )
        items = list(
            (
                await self.session.execute(
                    select(Workflow)
                    .where(*conditions)
                    .order_by(Workflow.created_at.desc(), Workflow.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars().all()
        )
        return items, total

    async def update_workflow(
        self,
        workflow_id: uuid.UUID,
        *,
        company_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
    ) -> Workflow:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        if workflow.status == WorkflowDefinitionStatus.ARCHIVED.value:
            raise ConflictError("Archived workflows cannot be modified.")

        if name is not None and name != workflow.name:
            if await self._name_exists(company_id, name, exclude_id=workflow.id):
                raise ConflictError(
                    f"A workflow named '{name}' already exists for this company.",
                    details=[{"field": "name", "value": name}],
                )
            workflow.name = name
        if description is not None:
            workflow.description = description

        if nodes is not None or edges is not None:
            # Structural edits create a fresh draft version; published versions
            # stay immutable so running executions keep a stable graph.
            existing_nodes, existing_edges = await load_version_graph(
                self.session, await self._draft_or_latest_version_id(workflow)
            )
            new_nodes = nodes if nodes is not None else [
                {
                    "node_key": node.node_key,
                    "node_type": node.node_type,
                    "name": node.name,
                    "configuration": node.configuration,
                    "input_mapping": node.input_mapping,
                    "output_mapping": node.output_mapping,
                    "execution_settings": node.execution_settings,
                }
                for node in existing_nodes
            ]
            new_edges = edges if edges is not None else [
                {"from_key": f, "to_key": t, "condition_expression": c} for f, t, c in existing_edges
            ]
            await self._write_draft_version(workflow, nodes=new_nodes, edges=new_edges)

        self.audit.record(
            event_type="workflow.updated",
            action="update",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={"changed": [k for k, v in (("name", name), ("description", description), ("graph", nodes is not None or edges is not None)) if v is not None]},
        )
        await self.session.flush()
        await self.session.refresh(workflow)
        return workflow

    async def delete_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> None:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        snapshot = {"name": workflow.name, "status": workflow.status}
        await self.session.delete(workflow)
        await self.session.flush()
        self.audit.record(
            event_type="workflow.deleted",
            action="delete",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata=snapshot,
        )

    # --- lifecycle ----------------------------------------------------------------
    async def validate_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID):
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        version_id = await self._draft_or_latest_version_id(workflow)
        nodes, edges = await load_version_graph(self.session, version_id)
        validator = GraphValidator(self.session, company_id=company_id)
        return await validator.validate(nodes, edges)

    async def publish_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> Workflow:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        self._require_transition(workflow, "published")

        version = await self._current_draft_version(workflow)
        graph = await self.validate_workflow(workflow_id, company_id=company_id)
        if not graph.is_valid:
            raise WorkflowValidationError(graph.issues)

        version.status = WorkflowVersionStatus.PUBLISHED.value
        workflow.status = WorkflowDefinitionStatus.PUBLISHED.value
        workflow.current_version_id = version.id
        self.audit.record(
            event_type="workflow.published",
            action="publish",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={"version": version.version},
        )
        await self.session.flush()
        await self.session.refresh(workflow)
        return workflow

    async def pause_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> Workflow:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        self._require_transition(workflow, "paused")
        workflow.status = WorkflowDefinitionStatus.PAUSED.value
        self.audit.record(
            event_type="workflow.paused",
            action="pause",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={},
        )
        await self.session.flush()
        await self.session.refresh(workflow)
        return workflow

    async def resume_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> Workflow:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        self._require_transition(workflow, "published")  # paused -> published
        workflow.status = WorkflowDefinitionStatus.PUBLISHED.value
        self.audit.record(
            event_type="workflow.resumed",
            action="resume",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={},
        )
        await self.session.flush()
        await self.session.refresh(workflow)
        return workflow

    async def archive_workflow(self, workflow_id: uuid.UUID, *, company_id: uuid.UUID) -> Workflow:
        workflow = await self.get_workflow(workflow_id, company_id=company_id)
        self._require_transition(workflow, "archived")
        workflow.status = WorkflowDefinitionStatus.ARCHIVED.value
        self.audit.record(
            event_type="workflow.archived",
            action="archive",
            company_id=company_id,
            resource_type=_WORKFLOW_RESOURCE,
            resource_id=workflow.id,
            metadata={},
        )
        await self.session.flush()
        await self.session.refresh(workflow)
        return workflow

    # --- versions -------------------------------------------------------------------
    async def _write_draft_version(
        self, workflow: Workflow, *, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
    ) -> WorkflowVersion:
        next_version = int(
            (
                await self.session.execute(
                    select(func.max(WorkflowVersion.version)).where(WorkflowVersion.workflow_id == workflow.id)
                )
            ).scalar_one()
            or 0
        ) + 1
        version = WorkflowVersion(workflow_id=workflow.id, version=next_version)
        self.session.add(version)
        await self.session.flush()
        for node in nodes:
            self.session.add(
                WorkflowNode(
                    version_id=version.id,
                    node_key=str(node["node_key"]),
                    node_type=str(node["node_type"]),
                    name=str(node.get("name") or node["node_key"]),
                    configuration=node.get("configuration") or {},
                    input_mapping=node.get("input_mapping") or {},
                    output_mapping=node.get("output_mapping") or {},
                    execution_settings=node.get("execution_settings") or {},
                )
            )
        for order, edge in enumerate(edges):
            self.session.add(
                WorkflowEdge(
                    version_id=version.id,
                    from_key=str(edge["from_key"]),
                    to_key=str(edge["to_key"]),
                    condition_expression=edge.get("condition_expression"),
                    edge_order=edge.get("edge_order", order),
                )
            )
        await self.session.flush()
        return version

    async def _draft_or_latest_version_id(self, workflow: Workflow) -> uuid.UUID:
        draft = await self._current_draft_version(workflow)
        if draft is not None:
            return draft.id
        if workflow.current_version_id is not None:
            return workflow.current_version_id
        raise ConflictError("Workflow has no versions yet.")

    async def _current_draft_version(self, workflow: Workflow) -> WorkflowVersion | None:
        result = await self.session.execute(
            select(WorkflowVersion)
            .where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.status == WorkflowVersionStatus.DRAFT.value,
            )
            .order_by(WorkflowVersion.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _published_version(self, workflow: Workflow) -> WorkflowVersion:
        if workflow.current_version_id is None:
            raise ConflictError("Workflow has no published version; publish it first.")
        version = await self.session.get(WorkflowVersion, workflow.current_version_id)
        if version is None or version.status != WorkflowVersionStatus.PUBLISHED.value:
            raise ConflictError("Workflow has no published version; publish it first.")
        return version

    # --- helpers -----------------------------------------------------------------
    async def _name_exists(
        self, company_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        conditions = [Workflow.company_id == company_id, Workflow.name == name]
        if exclude_id is not None:
            conditions.append(Workflow.id != exclude_id)
        result = await self.session.execute(select(Workflow.id).where(*conditions))
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _require_transition(workflow: Workflow, target: str) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(workflow.status, set())
        if target not in allowed:
            raise ConflictError(
                f"Cannot transition workflow from '{workflow.status}' to '{target}'.",
                details=[{"from": workflow.status, "to": target}],
            )


def workflow_published_version_id(workflow: Workflow) -> uuid.UUID | None:
    if workflow.status in (WorkflowDefinitionStatus.PUBLISHED.value, WorkflowDefinitionStatus.PAUSED.value):
        return workflow.current_version_id
    return None
