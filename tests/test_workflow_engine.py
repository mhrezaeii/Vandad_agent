"""Workflow engine tests: graph execution, conditionals, data propagation,
retry/timeout, cancellation, delay/resume, idempotency, tenant isolation.

These run the engine in-process against real PostgreSQL and real providers.
"""

from __future__ import annotations

import base64
import time
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError, NotFoundError
from app.integrations.providers import ProviderTransientError
from app.integrations.providers.base import ProviderResult
from app.models import (
    AgentTask,
    Company,
    CompanyProviderBudget,
    CredentialStorageType,
    Platform,
    PlatformConnection,
    ProviderUsageRecord,
    WorkflowExecution,
    WorkflowExecutionStep,
    WorkflowStatus,
)
from app.security.crypto import MasterKeyBox
from app.security.secret_value import SecretValue
from app.services.credential_service import CredentialService
from app.services.workflow_execution_service import WorkflowExecutionService
from app.workflows.engine import WorkflowEngine, _NodeSpec
from tests.conftest import TEST_MASTER_KEY_CONFIG
from tests.provider_mocks import ScriptedProvider

DIAGNOSTIC_CONFIG = {"expected_config_key": "x", "params": {"flag": True, "value": 7}}


def _keybox() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


def _diagnostic_node(key="start"):
    return {"node_key": key, "node_type": "trigger.manual", "name": key, "configuration": {}}


def _provider_node(key, connection_id, settings=None, input_mapping=None, output_mapping=None):
    return {
        "node_key": key,
        "node_type": "action.provider",
        "name": key,
        "configuration": {"connection_id": str(connection_id), "operation": "execute"},
        "input_mapping": input_mapping or {},
        "output_mapping": output_mapping or {},
        "execution_settings": settings or {},
    }


def _transform_node(key, mappings):
    return {
        "node_key": key,
        "node_type": "data.transform",
        "name": key,
        "configuration": {"mappings": mappings},
    }


def _condition_node(key, expression):
    return {
        "node_key": key,
        "node_type": "control.condition",
        "name": key,
        "configuration": {"expression": expression},
    }


def _delay_node(key, seconds):
    return {"node_key": key, "node_type": "control.delay", "name": key, "configuration": {"seconds": seconds}}


def _complete_node(key="finish", outcome="completed"):
    return {
        "node_key": key,
        "node_type": "control.complete",
        "name": key,
        "configuration": {"outcome": outcome},
    }


def _edge(frm, to, condition=None):
    return {"from_key": frm, "to_key": to, "condition_expression": condition}


async def _steps(db_session, execution_id) -> list[WorkflowExecutionStep]:
    return list(
        (
            await db_session.execute(
                select(WorkflowExecutionStep)
                .where(WorkflowExecutionStep.execution_id == execution_id)
                .order_by(WorkflowExecutionStep.started_at, WorkflowExecutionStep.attempt)
            )
        ).scalars().all()
    )


async def _make_connection(
    db_session,
    company_id,
    *,
    provider="diagnostic",
    configuration=None,
) -> PlatformConnection:
    result = await db_session.execute(
        select(Platform).where(Platform.company_id == company_id).limit(1)
    )
    platform = result.scalar_one()
    connection = PlatformConnection(
        company_id=company_id,
        platform_id=platform.id,
        name=f"conn-{uuid.uuid4().hex[:8]}",
        provider=provider,
        status="connected",
        configuration=configuration if configuration is not None else dict(DIAGNOSTIC_CONFIG),
    )
    db_session.add(connection)
    await db_session.commit()
    return connection


@pytest.fixture
def wf_engine(db_session, engine_pair):
    registry, executor = engine_pair
    return WorkflowEngine(db_session, registry, executor, _keybox())


@pytest.fixture
def execution_factory(db_session, engine_pair):
    registry, executor = engine_pair

    async def _make(workflow, *, company_id, input_metadata=None, idempotency_key=None):
        service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=None)
        scope = SimpleNamespace(company_id=company_id)
        execution, created = await service.execute_workflow(
            workflow.id,
            scope=scope,
            input_metadata=input_metadata,
            idempotency_key=idempotency_key,
            enqueue=False,
        )
        return execution, created, service

    return _make


async def test_linear_execution_completes(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id, [_diagnostic_node(), _complete_node()], [_edge("start", "finish")]
    )
    execution, created, _ = await execution_factory(workflow, company_id=company.id)
    assert created

    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"
    assert execution.status == WorkflowStatus.COMPLETED.value
    assert execution.started_at is not None
    assert execution.completed_at is not None

    steps = await _steps(db_session, execution.id)
    assert [step.node_key for step in steps] == ["start", "finish"]
    assert all(step.status == "completed" for step in steps)
    assert all(step.duration_ms is not None for step in steps)


async def test_data_propagation_through_transform(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    conn = await _make_connection(db_session, company.id)
    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _provider_node("run", conn.id),
            _transform_node(
                "shape",
                {"summary": {"$template": "flag={{ nodes.run.output.echo.flag }} v={{ nodes.run.output.echo.value }}"}},
            ),
            _complete_node(),
        ],
        [_edge("start", "run"), _edge("run", "shape"), _edge("shape", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"

    steps = await _steps(db_session, execution.id)
    shape_step = next(step for step in steps if step.node_key == "shape")
    # The diagnostic provider echoes its params; input_mapping fed them through.
    assert shape_step.output == {"summary": "flag=True v=7"}


async def test_input_mapping_overrides_provider_params(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    conn = await _make_connection(db_session, company.id)
    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _provider_node("run", conn.id, input_mapping={"params.value": "trigger.n"}),
            _complete_node(),
        ],
        [_edge("start", "run"), _edge("run", "finish")],
    )
    execution, _created, _ = await execution_factory(
        workflow, company_id=company.id, input_metadata={"n": 99}
    )
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"

    steps = await _steps(db_session, execution.id)
    run_step = next(step for step in steps if step.node_key == "run")
    assert run_step.output["echo"]["value"] == 99


async def test_conditional_branching(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    conn = await _make_connection(db_session, company.id)
    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _provider_node("run", conn.id, input_mapping={"params.flag": "trigger.flag"}),
            _condition_node("branch", "nodes.run.output.echo.flag == True"),
            _transform_node("yes_path", {"taken": "yes"}),
            _transform_node("no_path", {"taken": "no"}),
        ],
        [
            _edge("start", "run"),
            _edge("run", "branch"),
            _edge("branch", "yes_path", "nodes.branch.output.matched"),
            _edge("branch", "no_path", "not nodes.branch.output.matched"),
        ],
    )
    execution, _created, _ = await execution_factory(
        workflow, company_id=company.id, input_metadata={"flag": True}
    )
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"

    ran = {step.node_key for step in await _steps(db_session, execution.id)}
    assert "yes_path" in ran
    assert "no_path" not in ran


async def test_transient_retry_then_success(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform, fast_retry_policy
):
    company, _platform = company_and_platform
    flaky = ScriptedProvider()
    flaky.queue(ProviderTransientError("blip", code="blip"), ProviderTransientError("blip2", code="blip"))
    wf_engine.registry.register(flaky)
    conn = await _make_connection(db_session, company.id, provider="scripted")

    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _provider_node("run", conn.id, settings={"max_retries": 2, "backoff_seconds": 0}),
            _complete_node(),
        ],
        [_edge("start", "run"), _edge("run", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"

    run_step = next(step for step in await _steps(db_session, execution.id) if step.node_key == "run")
    assert run_step.attempt == 3
    assert flaky.calls == 3


async def test_permanent_failure_fails_execution(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    conn = await _make_connection(db_session, company.id, configuration={})
    workflow = await workflow_factory(
        company.id,
        [_diagnostic_node("start"), _provider_node("run", conn.id), _complete_node()],
        [_edge("start", "run"), _edge("run", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "failed"
    assert execution.status == WorkflowStatus.FAILED.value
    assert execution.completed_at is not None

    run_step = next(step for step in await _steps(db_session, execution.id) if step.node_key == "run")
    assert run_step.status == "failed"
    assert run_step.error_code == "invalid_configuration"
    # The failed node's downstream node never ran.
    assert all(step.node_key != "finish" for step in await _steps(db_session, execution.id))


async def test_continue_on_error_lets_downstream_run(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    conn = await _make_connection(db_session, company.id, configuration={})
    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _provider_node("run", conn.id, settings={"continue_on_error": True}),
            _transform_node("after", {"x": "y"}),
        ],
        [_edge("start", "run"), _edge("run", "after")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"
    ran = {step.node_key for step in await _steps(db_session, execution.id)}
    assert "after" in ran


async def test_node_timeout_is_bounded(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    sleeper = ScriptedProvider()
    sleeper.queue(30.0, 30.0)
    wf_engine.registry.register(sleeper)
    conn = await _make_connection(db_session, company.id, provider="scripted")

    workflow = await workflow_factory(
        company.id,
        [_diagnostic_node("start"), _provider_node("run", conn.id, settings={"timeout_seconds": 0.2})],
        [_edge("start", "run")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    execution.timeout_seconds = 20
    started = time.monotonic()
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert time.monotonic() - started < 15
    assert result.outcome == "failed"


async def test_execution_level_timeout(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    sleeper = ScriptedProvider()
    sleeper.queue(30.0)
    wf_engine.registry.register(sleeper)
    conn = await _make_connection(db_session, company.id, provider="scripted")

    workflow = await workflow_factory(
        company.id,
        [_diagnostic_node("start"), _provider_node("run", conn.id, settings={"timeout_seconds": 20})],
        [_edge("start", "run")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    execution.timeout_seconds = 1
    started = time.monotonic()
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert time.monotonic() - started < 10
    assert result.outcome == "timed_out"
    assert execution.status == WorkflowStatus.TIMED_OUT.value


async def test_idempotency_key_returns_existing_execution(
    db_session, engine_pair, workflow_factory, company_and_platform
):
    registry, executor = engine_pair
    company, _platform = company_and_platform
    workflow = await workflow_factory(company.id, [_diagnostic_node()], [])
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=None)
    scope = SimpleNamespace(company_id=company.id)

    first, created1 = await service.execute_workflow(
        workflow.id, scope=scope, idempotency_key="order-42", enqueue=False
    )
    second, created2 = await service.execute_workflow(
        workflow.id, scope=scope, idempotency_key="order-42", enqueue=False
    )
    assert created1 and not created2
    assert first.id == second.id


async def test_cancellation_between_nodes(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id,
        [_diagnostic_node("start"), _transform_node("mid", {"x": "y"}), _complete_node()],
        [_edge("start", "mid"), _edge("mid", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    execution.cancel_requested = True  # cancel request arrives mid-run
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "cancelled"
    assert execution.status == WorkflowStatus.CANCELLED.value


async def test_delay_pauses_and_resumes(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id,
        [
            _diagnostic_node("start"),
            _delay_node("wait", 1),
            _transform_node("after", {"x": "y"}),
            _complete_node(),
        ],
        [_edge("start", "wait"), _edge("wait", "after"), _edge("after", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)

    first = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert first.outcome == "waiting"
    assert execution.status == WorkflowStatus.WAITING.value
    assert execution.context_snapshot is not None
    assert execution.context_snapshot["delay_node_key"] == "wait"

    second = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert second.outcome == "completed"
    assert execution.status == WorkflowStatus.COMPLETED.value
    assert execution.context_snapshot is None

    wait_step = next(step for step in await _steps(db_session, execution.id) if step.node_key == "wait")
    assert wait_step.status == "completed"
    assert "waited_seconds" in (wait_step.output or {})


async def test_cross_company_execution_not_found(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await workflow_factory(company.id, [_diagnostic_node()], [])
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)

    stranger = Company(name="Other Co", slug="other-co-wf")
    db_session.add(stranger)
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await wf_engine.run_execution(execution.id, company_id=stranger.id)


async def test_credential_flows_to_provider_but_not_to_output(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform, key_box
):
    """Encrypted credentials reach the provider boundary — and nowhere else."""
    company, platform = company_and_platform
    secret_value = "engine-flow-secret-xyz"
    credential_service = CredentialService(db_session, key_box)
    reference = await credential_service.create_credential(
        platform_id=platform.id,
        credential_type="api_key",
        secret_reference="vault://wf/secret",
        storage_type=CredentialStorageType.ENCRYPTED,
        value=SecretValue(secret_value),
    )

    captured: list[str] = []

    class CapturingProvider(ScriptedProvider):
        provider_id = "wf_capturing"

        async def execute(self, context):
            captured.append(context.credential.reveal() if context.credential else "")
            return await super().execute(context)

    wf_engine.registry.register(CapturingProvider())
    conn = await _make_connection(db_session, company.id, provider="wf_capturing")
    conn.credential_reference_id = reference.id
    await db_session.commit()

    workflow = await workflow_factory(
        company.id,
        [_diagnostic_node("start"), _provider_node("run", conn.id), _complete_node()],
        [_edge("start", "run"), _edge("run", "finish")],
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    result = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert result.outcome == "completed"
    assert captured == [secret_value]

    for step in await _steps(db_session, execution.id):
        assert secret_value not in str(step.output)
        assert secret_value not in str(step.error_message)


async def test_execute_workflow_requires_published(
    db_session, engine_pair, workflow_factory, company_and_platform
):
    registry, executor = engine_pair
    company, _platform = company_and_platform
    draft = await workflow_factory(company.id, [_diagnostic_node()], [], publish=False)
    service = WorkflowExecutionService(db_session, registry, executor, _keybox(), queue=None)
    scope = SimpleNamespace(company_id=company.id)

    with pytest.raises(ConflictError):
        await service.execute_workflow(draft.id, scope=scope, enqueue=False)


async def test_engine_is_idempotent_for_terminal_executions(
    db_session, wf_engine, workflow_factory, execution_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await workflow_factory(
        company.id, [_diagnostic_node(), _complete_node()], [_edge("start", "finish")]
    )
    execution, _created, _ = await execution_factory(workflow, company_id=company.id)
    first = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert first.outcome == "completed"
    steps_after_first = len(await _steps(db_session, execution.id))

    # Running a terminal execution again must be a no-op.
    second = await wf_engine.run_execution(execution.id, company_id=company.id)
    assert second.outcome == "completed"
    assert len(await _steps(db_session, execution.id)) == steps_after_first


async def test_workflow_llm_operation_is_bound_to_agent_task_spend_ceiling(
    db_session, wf_engine, company_and_platform, monkeypatch
):
    company, _ = company_and_platform
    connection = await _make_connection(
        db_session,
        company.id,
        provider="llm_openai",
        configuration={"model": "gpt-4.1-mini", "params": {}},
    )
    task = AgentTask(
        company_id=company.id,
        agent_key="content",
        capability="content.scenario.generate",
        title="Budgeted script workflow",
        status="running",
        input_payload={},
        spend_ceiling_usd=Decimal("0.50"),
    )
    db_session.add_all([
        task,
        CompanyProviderBudget(
            company_id=company.id,
            provider_id="llm_openai",
            monthly_limit_usd=Decimal("10.00"),
            per_operation_limit_usd=Decimal("1.00"),
            enabled=True,
        ),
    ])
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="budgeted-script-workflow",
        workflow_type="custom",
        status="running",
        input_metadata={"agent_task_id": str(task.id)},
    )
    db_session.add(execution)
    await db_session.commit()

    adapter = wf_engine.registry.get("llm_openai")
    calls = 0

    async def fake_execute(context):
        nonlocal calls
        calls += 1
        return ProviderResult(
            provider_id="llm_openai",
            success=True,
            message="Script generated.",
            metadata={"usage": {"cost_usd": "0.20", "currency": "USD"}},
        )

    monkeypatch.setattr(adapter, "execute", fake_execute)
    node = SimpleNamespace(
        configuration={"connection_id": str(connection.id), "operation": "execute"},
        node_key="script_generation",
        output_mapping={},
    )
    spec = _NodeSpec(node=node, settings={"timeout_seconds": 30, "idempotent": True})

    with pytest.raises(ConflictError, match="agent task spend ceiling"):
        await wf_engine._run_provider_action(
            execution, spec, {}, timeout_seconds=30
        )
    assert calls == 0

    task.spend_ceiling_usd = Decimal("2.00")
    await db_session.commit()
    result = await wf_engine._run_provider_action(
        execution, spec, {}, timeout_seconds=30
    )
    assert result["message"] == "Script generated."
    assert calls == 1

    usage = await db_session.scalar(
        select(ProviderUsageRecord).where(ProviderUsageRecord.task_id == task.id)
    )
    assert usage is not None
    assert usage.status == "settled"
    assert usage.actual_cost_usd == Decimal("0.20")
