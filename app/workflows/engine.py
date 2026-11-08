"""Workflow execution engine.

Executes a validated workflow graph against real Phase 2 providers:

- topological traversal from the trigger node, OR-semantics for incoming edges
- declarative input/output mapping and template resolution (no code execution)
- condition nodes and edge conditions evaluated by the safe expression engine
- delay nodes pause the execution (WAITING) and resume from a context snapshot
- per-node retry (transient only), timeout (via ProviderExecutor), and
  continue-on-error handling
- execution-level deadline (TIMED_OUT) and cancellation (CANCELLED)
- every step persisted with attempts; every log line redacted

The engine runs inside one unit of work owned by its caller (API request or
worker job) and never commits itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.correlation import get_correlation_id
from app.core.redis import RedisConnectionManager  # noqa: F401 - re-exported for worker wiring
from app.integrations.providers import (
    ProviderCapability,
    ProviderError,
    ProviderExecutor,
    ProviderRegistry,
    ProviderTransientError,
    RetryPolicy,
)
from app.models import (
    ConnectionStatus,
    PlatformConnection,
    WorkflowExecution,
    WorkflowExecutionLog,
    WorkflowExecutionStep,
    WorkflowLogLevel,
    WorkflowNode,
    WorkflowStepStatus,
)
from app.security.crypto import MasterKeyBox
from app.services.credential_service import CredentialService
from app.workflows.expressions import ExpressionError, safe_evaluate
from app.workflows.mapping import (
    interpolate_template,
    project_output,
    resolve_input_mapping,
)
from app.workflows.nodes import (
    MAX_DELAY_SECONDS,
    MAX_NODE_RETRIES,
    NodeType,
    TRIGGER_NODE_TYPES,
)
from app.workflows.validation import load_version_graph

logger = logging.getLogger("app.workflow_engine")

EXECUTION_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TRANSIENT_SLEEP = 8.0


class ExecutionOutcome:
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class _DelayRequested(Exception):
    """Internal signal: the node wants to pause the execution."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        super().__init__(f"delay {seconds}s")


@dataclass
class EngineResult:
    outcome: str
    execution: WorkflowExecution
    steps_run: int = 0


@dataclass
class _NodeSpec:
    node: WorkflowNode
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def timeout_seconds(self) -> float:
        return float(self.settings.get("timeout_seconds", 60))

    @property
    def max_retries(self) -> int:
        return int(self.settings.get("max_retries", 0) or 0)

    @property
    def backoff_seconds(self) -> float:
        return float(self.settings.get("backoff_seconds", 1.0) or 0)

    @property
    def continue_on_error(self) -> bool:
        return bool(self.settings.get("continue_on_error", False))

    @property
    def idempotent(self) -> bool:
        return bool(self.settings.get("idempotent", True))


class WorkflowEngine:
    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box

    # ------------------------------------------------------------------ public
    async def run_execution(self, execution_id: uuid.UUID, *, company_id: uuid.UUID) -> EngineResult:
        execution = await self._load_execution(execution_id, company_id)

        if execution.status in ("completed", "failed", "cancelled", "timed_out", "succeeded"):
            return EngineResult(outcome=execution.status, execution=execution)  # idempotent no-op

        nodes, edges = await load_version_graph(self.session, execution.workflow_version_id)
        node_map: dict[str, WorkflowNode] = {node.node_key: node for node in nodes}
        specs = {key: _NodeSpec(node=node, settings={**node.execution_settings}) for key, node in node_map.items()}
        adjacency: dict[str, list[tuple[str, str | None]]] = {}
        for from_key, to_key, condition in edges:
            adjacency.setdefault(from_key, []).append((to_key, condition))

        # Resume state or fresh start.
        snapshot = execution.context_snapshot or {}
        context: dict[str, Any] = {
            "trigger": dict(execution.input_metadata or {}),
            "nodes": dict(snapshot.get("nodes", {})),
        }
        if execution.started_at is None:
            execution.started_at = datetime.now(UTC)
        execution.status = "running"
        execution.worker_id = snapshot.get("worker_id")
        execution.heartbeat_at = datetime.now(UTC)
        await self.session.flush()
        self._log(execution, "info", "Execution started." if not snapshot else "Execution resumed from waiting state.")

        resume_ready: list[str] = list(snapshot.get("resume_ready", []))
        deadline = time.monotonic() + float(execution.timeout_seconds or EXECUTION_DEFAULT_TIMEOUT_SECONDS)

        # The delay node that paused this execution completes on resume.
        resuming_delay_key = snapshot.get("delay_node_key")

        ready: list[str] = []
        if resuming_delay_key:
            ready = resume_ready
            delay_step = await self._latest_step(execution.id, resuming_delay_key)
            if delay_step is not None and delay_step.status == "waiting":
                waited = (datetime.now(UTC) - delay_step.started_at).total_seconds() if delay_step.started_at else 0
                delay_step.status = WorkflowStepStatus.COMPLETED.value
                delay_step.completed_at = datetime.now(UTC)
                delay_step.duration_ms = int(waited * 1000)
                delay_step.output = {"waited_seconds": round(waited, 2)}
        elif not context["nodes"]:
            ready = [execution_trigger_key(nodes)]

        steps_run = 0
        outcome: str | None = None

        while ready:
            # Cancellation is checked between nodes (fresh read).
            await self.session.refresh(execution, attribute_names=["cancel_requested"])
            if execution.cancel_requested:
                outcome = ExecutionOutcome.CANCELLED
                self._log(execution, "warning", "Execution cancelled by request.")
                break
            if time.monotonic() > deadline:
                outcome = ExecutionOutcome.TIMED_OUT
                self._log(execution, "error", "Execution exceeded its overall time budget.")
                break

            node_key = ready.pop(0)
            spec = specs.get(node_key)
            if spec is None:  # defensive: edges always validated
                continue
            if context["nodes"].get(node_key, {}).get("status") in ("completed", "skipped"):
                continue  # already handled (OR-join may deliver a node twice)

            steps_run += 1
            step_outcome = await self._execute_node(execution, spec, context, deadline)

            if step_outcome == ExecutionOutcome.WAITING:
                outcome = ExecutionOutcome.WAITING
                break

            node_status = context["nodes"].get(node_key, {}).get("status")
            if node_status == "failed":
                if spec.continue_on_error:
                    self._log(execution, "warning", f"Node '{node_key}' failed; continue_on_error is set.")
                else:
                    outcome = ExecutionOutcome.FAILED
                    break

            if node_status == "completed":
                for target, condition in adjacency.get(node_key, []):
                    if condition and not self._evaluate_condition(condition, context, execution):
                        self._log(execution, "info", f"Edge to '{target}' not taken (condition false).")
                        continue
                    ready.append(target)
            elif node_status == "skipped":
                continue

        if outcome is None:
            outcome = ExecutionOutcome.COMPLETED

        await self._finish_execution(execution, outcome, context, steps_run)
        return EngineResult(outcome=outcome, execution=execution, steps_run=steps_run)

    # ----------------------------------------------------------------- nodes
    async def _execute_node(
        self,
        execution: WorkflowExecution,
        spec: _NodeSpec,
        context: dict[str, Any],
        deadline: float,
    ) -> str:
        node = spec.node
        started_at = datetime.now(UTC)
        step = WorkflowExecutionStep(
            execution_id=execution.id,
            node_key=node.node_key,
            node_type=node.node_type,
            status=WorkflowStepStatus.RUNNING.value,
            attempt=1,
            started_at=started_at,
        )
        self.session.add(step)
        await self.session.flush()
        self._log(execution, "info", f"Node '{node.node_key}' ({node.node_type}) started.", step=step)

        attempts_allowed = spec.max_retries + 1
        last_error: ProviderError | None = None

        for attempt in range(1, attempts_allowed + 1):
            step.attempt = attempt
            try:
                output = await self._dispatch_node(execution, spec, context)
            except _DelayRequested as delay:
                # Pause the execution: step stays 'waiting', context snapshot is
                # persisted and the worker re-enqueues with an eta.
                step.status = WorkflowStepStatus.WAITING.value
                self._log(
                    execution,
                    "info",
                    f"Node '{node.node_key}' delaying execution for {delay.seconds:.0f}s.",
                    step=step,
                )
                successors = await self._successor_keys(execution.workflow_version_id, node.node_key)
                execution.context_snapshot = {
                    "nodes": context["nodes"],
                    "resume_ready": successors,
                    "delay_node_key": node.node_key,
                }
                execution.status = "waiting"
                execution.output_metadata = {"delay_seconds": delay.seconds}
                await self.session.flush()
                return ExecutionOutcome.WAITING
            except ProviderTransientError as exc:
                last_error = exc
                if attempt < attempts_allowed and time.monotonic() < deadline:
                    delay = min(spec.backoff_seconds * (2 ** (attempt - 1)), _MAX_TRANSIENT_SLEEP)
                    self._log(
                        execution,
                        "warning",
                        f"Node '{node.node_key}' transient failure ({exc.code}); retrying in {delay:.1f}s.",
                        step=step,
                    )
                    await asyncio.sleep(delay)
                    continue
            except ProviderError as exc:
                last_error = exc
            except ExpressionError as exc:
                last_error = ProviderError(f"Expression error: {exc}", code="invalid_expression")
            else:
                context["nodes"][node.node_key] = {"status": "completed", "output": output}
                step.status = WorkflowStepStatus.COMPLETED.value
                step.completed_at = datetime.now(UTC)
                step.duration_ms = int((step.completed_at - started_at).total_seconds() * 1000)
                step.output = _safe_output(output)
                self._log(execution, "info", f"Node '{node.node_key}' completed.", step=step)
                await self.session.flush()
                return ExecutionOutcome.COMPLETED

        # All attempts exhausted (or a permanent failure).
        error = last_error or ProviderError("The node failed.", code="node_failure")
        context["nodes"][node.node_key] = {"status": "failed", "output": None}
        step.status = WorkflowStepStatus.FAILED.value
        step.completed_at = datetime.now(UTC)
        step.duration_ms = int((step.completed_at - started_at).total_seconds() * 1000)
        step.error_code = error.code
        step.error_message = error.message[:1000]
        self._log(execution, "error", f"Node '{node.node_key}' failed: {error.message}", step=step)
        await self.session.flush()
        return ExecutionOutcome.FAILED

    async def _dispatch_node(
        self, execution: WorkflowExecution, spec: _NodeSpec, context: dict[str, Any]
    ) -> dict[str, Any]:
        node = spec.node
        params = resolve_input_mapping(node.input_mapping, context)

        if node.node_type == NodeType.TRIGGER_MANUAL:
            return {"trigger": context["trigger"]}

        if node.node_type == NodeType.CONTROL_CONDITION:
            expression = node.configuration.get("expression", "")
            matched = bool(safe_evaluate(expression, variables={
                "trigger": context["trigger"],
                "nodes": context["nodes"],
                "node": {"input": params},
            }))
            return {"matched": matched}

        if node.node_type == NodeType.DATA_TRANSFORM:
            mappings = node.configuration.get("mappings", {})
            return resolve_input_mapping(mappings, context)

        if node.node_type == NodeType.CONTROL_DELAY:
            return await self._handle_delay(execution, spec, context, params)

        if node.node_type == NodeType.CONTROL_COMPLETE:
            outcome = node.configuration.get("outcome", "completed")
            execution.output_metadata = {"outcome": outcome, "message": params.get("message", "")}
            if outcome == "failed":
                raise ProviderError(
                    str(params.get("message") or "Workflow reported failure."),
                    code="workflow_failed",
                )
            return {"outcome": "completed", "message": params.get("message", "")}

        if node.node_type == NodeType.ACTION_PROVIDER:
            return await self._run_provider_action(execution, spec, params)

        raise ProviderError(f"Unsupported node type at runtime: {node.node_type}", code="invalid_node_type")

    async def _handle_delay(
        self, execution: WorkflowExecution, spec: _NodeSpec, context: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        """Signal a pause; the engine persists the snapshot (see _execute_node).

        The worker re-enqueues the job with an eta; the resumed run completes
        this node and continues from its successors.
        """
        node = spec.node
        seconds = min(float(node.configuration.get("seconds", 1)), MAX_DELAY_SECONDS)
        override = params.get("seconds")
        if isinstance(override, (int, float)) and 0 < float(override) <= MAX_DELAY_SECONDS:
            seconds = float(override)
        raise _DelayRequested(seconds)

    async def _run_provider_action(
        self, execution: WorkflowExecution, spec: _NodeSpec, params: dict[str, Any]
    ) -> dict[str, Any]:
        node = spec.node
        connection_id = node.configuration.get("connection_id")
        operation = node.configuration.get("operation", "execute")
        connection = await self.session.get(PlatformConnection, uuid.UUID(str(connection_id)))
        if connection is None or connection.company_id != execution.company_id:
            raise ProviderError(
                "Connection does not exist or belongs to another company.",
                code="invalid_connection_reference",
            )
        if connection.status not in (ConnectionStatus.CONNECTED.value, ConnectionStatus.CONFIGURED.value):
            raise ProviderError(
                f"Connection is not usable (status '{connection.status}').",
                code="connection_not_ready",
            )

        adapter = self.registry.get(connection.provider)
        if ProviderCapability.EXECUTION not in adapter.capabilities and operation == "execute":
            raise ProviderError(
                "Provider does not support the execute operation.",
                code="operation_unsupported",
            )

        credential = None
        credential_type = None
        secret_reference = None
        if connection.credential_reference_id is not None:
            resolved = await CredentialService(self.session, self._keys).resolve_credential(
                connection.credential_reference_id, company_id=execution.company_id
            )
            credential, credential_type, secret_reference = (
                resolved.payload,
                resolved.credential_type,
                resolved.secret_reference,
            )

        reserved = {"connection_id", "operation"}
        action_params = {key: value for key, value in params.items() if key not in reserved}
        context = self.executor.build_context(
            adapter=adapter,
            connection_id=connection.id,
            company_id=execution.company_id,
            platform_id=connection.platform_id,
            configuration={**connection.configuration, **action_params},
            credential=credential,
            credential_type=credential_type,
            secret_reference=secret_reference,
            operation=operation,
            timeout_seconds=spec.timeout_seconds,
        )
        operation_callable = {
            "execute": adapter.execute,
            "validate_connection": adapter.validate_connection,
            "check_health": adapter.check_health,
        }[operation]
        outcome = await self.executor.run(
            adapter, context, operation_callable, idempotent=spec.idempotent
        )
        if not outcome.success:
            error = outcome.error or ProviderError("The provider operation failed.")
            raise error
        result = outcome.result
        raw_output = {
            "message": result.message,
            "latency_ms": result.latency_ms,
            **(result.metadata or {}),
        }
        return project_output(node.output_mapping, raw_output)

    # -------------------------------------------------------------- lifecycle
    async def _finish_execution(
        self,
        execution: WorkflowExecution,
        outcome: str,
        context: dict[str, Any],
        steps_run: int,
    ) -> None:
        now = datetime.now(UTC)
        status_map = {
            ExecutionOutcome.COMPLETED: "completed",
            ExecutionOutcome.FAILED: "failed",
            ExecutionOutcome.CANCELLED: "cancelled",
            ExecutionOutcome.TIMED_OUT: "timed_out",
            ExecutionOutcome.WAITING: "waiting",
        }
        execution.status = status_map[outcome]
        execution.heartbeat_at = None if outcome != ExecutionOutcome.WAITING else execution.heartbeat_at
        if outcome != ExecutionOutcome.WAITING:
            execution.completed_at = now
            execution.context_snapshot = None if outcome != ExecutionOutcome.WAITING else execution.context_snapshot
            execution.output_metadata = {
                **(execution.output_metadata or {}),
                "steps_run": steps_run,
                "finished_at": now.isoformat(),
            }
        self._log(execution, "info", f"Execution outcome: {execution.status}.")
        await self.session.flush()

    # --------------------------------------------------------------- helpers
    def _evaluate_condition(
        self, condition: str, context: dict[str, Any], execution: WorkflowExecution
    ) -> bool:
        try:
            return bool(
                safe_evaluate(condition, variables={
                    "trigger": context["trigger"],
                    "nodes": context["nodes"],
                })
            )
        except ExpressionError as exc:
            self._log(execution, "warning", f"Invalid edge condition: {exc}")
            return False

    async def _load_execution(self, execution_id: uuid.UUID, company_id: uuid.UUID) -> WorkflowExecution:
        execution = await self.session.get(WorkflowExecution, execution_id)
        if execution is None or execution.company_id != company_id:
            from app.core.exceptions import NotFoundError

            raise NotFoundError(
                "Execution not found.", details=[{"field": "execution_id", "value": str(execution_id)}]
            )
        return execution

    async def _latest_step(self, execution_id: uuid.UUID, node_key: str) -> WorkflowExecutionStep | None:
        result = await self.session.execute(
            select(WorkflowExecutionStep)
            .where(
                WorkflowExecutionStep.execution_id == execution_id,
                WorkflowExecutionStep.node_key == node_key,
            )
            .order_by(WorkflowExecutionStep.attempt.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _successor_keys(self, version_id: uuid.UUID, node_key: str) -> list[str]:
        from app.models import WorkflowEdge

        rows = await self.session.execute(
            select(WorkflowEdge.to_key).where(
                WorkflowEdge.version_id == version_id, WorkflowEdge.from_key == node_key
            )
        )
        return [row for row in rows.scalars().all()]

    def _log(
        self,
        execution: WorkflowExecution,
        level: str,
        message: str,
        *,
        step: WorkflowExecutionStep | None = None,
    ) -> None:
        self.session.add(
            WorkflowExecutionLog(
                execution_id=execution.id,
                step_id=step.id if step else None,
                node_key=step.node_key if step else None,
                level=level,
                message=message[:1000],
                correlation_id=get_correlation_id(),
            )
        )


def execution_trigger_key(nodes: list[WorkflowNode]) -> str:
    for node in nodes:
        if node.node_type in TRIGGER_NODE_TYPES:
            return node.node_key
    raise ValueError("workflow version has no trigger node")


def _safe_output(output: dict[str, Any] | None) -> dict[str, Any] | None:
    """Output is provider-authored and sanitized by contract; keep it bounded."""
    if output is None:
        return None
    return {key: value for key, value in list(output.items())[:50]}
