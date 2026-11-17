"""Connection lifecycle tests: transitions, validation outcomes, recovery."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.exceptions import ConflictError
from app.integrations.providers import (
    ProviderExecutor,
    ProviderPermanentError,
    RetryPolicy,
)
from app.models import AuditEvent, ConnectionEvent, ConnectionStatus
from app.security.crypto import MasterKeyBox
from app.services.connection_lifecycle import ALLOWED_TRANSITIONS, can_transition
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
from tests.conftest import TEST_MASTER_KEY_CONFIG
from tests.provider_mocks import ScriptedProvider


@pytest.fixture
def key_box() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): __import__("base64").b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


@pytest.fixture
def connection_service(db_session, key_box):
    from app.integrations.providers import build_default_registry

    return ConnectionService(db_session, build_default_registry(), master_key_box=key_box)


async def _make_connection(
    connection_service, platform, provider="diagnostic", configuration=None, name="primary"
):
    return await connection_service.create_connection(
        company_id=platform.company_id,
        platform_id=platform.id,
        name=name,
        provider=provider,
        configuration=configuration if configuration is not None else {"expected_config_key": "x"},
    )


def test_state_machine_covers_all_statuses():
    statuses = {status.value for status in ConnectionStatus}
    assert set(ALLOWED_TRANSITIONS) == statuses


def test_explicit_valid_transitions():
    assert can_transition("draft", "configured")
    assert can_transition("draft", "validating")
    assert can_transition("configured", "validating")
    assert can_transition("validating", "connected")
    assert can_transition("validating", "degraded")
    assert can_transition("validating", "error")
    assert can_transition("connected", "degraded")
    assert can_transition("degraded", "validating")
    assert can_transition("error", "validating")
    assert can_transition("disconnected", "validating")


def test_explicit_invalid_transitions():
    assert not can_transition("draft", "connected")
    assert not can_transition("validating", "disconnected")
    assert not can_transition("validating", "draft")
    assert not can_transition("connected", "draft")
    assert not can_transition("error", "connected")
    assert not can_transition("unknown_status", "connected")


async def test_create_connection_starts_in_draft_or_configured(
    connection_service, company_and_platform
):
    _, platform = company_and_platform

    empty = await _make_connection(connection_service, platform, name="no-config", configuration={})
    assert empty.status == ConnectionStatus.DRAFT.value

    configured = await _make_connection(connection_service, platform, name="with-config")
    assert configured.status == ConnectionStatus.CONFIGURED.value


async def test_duplicate_connection_name_rejected(connection_service, company_and_platform):
    _, platform = company_and_platform
    await _make_connection(connection_service, platform)
    with pytest.raises(ConflictError):
        await _make_connection(connection_service, platform)


async def test_unknown_provider_rejected_at_creation(connection_service, company_and_platform):
    _, platform = company_and_platform
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await connection_service.create_connection(
            company_id=platform.company_id,
            platform_id=platform.id,
            name="bad-provider",
            provider="not-registered",
            configuration={},
        )


async def test_successful_validation_connects(
    db_session, connection_service, key_box, company_and_platform
    ):
    _, platform = company_and_platform
    connection = await _make_connection(connection_service, platform)

    validation = ConnectionValidationService(
        db_session,
        connection_service.registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    result = await validation.test_connection(connection.id, company_id=platform.company_id)

    assert result.success
    assert result.status_after == ConnectionStatus.CONNECTED.value

    updated = await connection_service.connections.get(connection.id)
    assert updated.last_connected_at is not None
    assert updated.consecutive_failures == 0
    assert updated.activated_at is not None

    events = (
        await db_session.execute(
            select(ConnectionEvent)
            .where(ConnectionEvent.connection_id == connection.id)
            .order_by(ConnectionEvent.created_at)
        )
    ).scalars().all()
    assert [event.to_status for event in events] == [
        ConnectionStatus.CONFIGURED.value,
        ConnectionStatus.VALIDATING.value,
        ConnectionStatus.CONNECTED.value,
    ]


async def test_failed_validation_marks_error_and_counts_failures(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    # Configuration missing the required key -> permanent validation failure.
    connection = await _make_connection(connection_service, platform, configuration={})

    validation = ConnectionValidationService(
        db_session,
        connection_service.registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    result = await validation.test_connection(connection.id, company_id=platform.company_id)

    assert not result.success
    assert result.status_after == ConnectionStatus.ERROR.value
    assert result.error_code == "invalid_configuration"

    updated = await connection_service.connections.get(connection.id)
    assert updated.consecutive_failures == 1
    assert updated.last_failure_at is not None
    assert updated.last_error_code == "invalid_configuration"
    assert "expected_config_key" in updated.last_error_message


async def test_provider_timeout_is_bounded_and_classified(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    registry = connection_service.registry
    sleeper = ScriptedProvider()
    # Every attempt sleeps far beyond the budget; nothing ever succeeds.
    sleeper.queue(30.0, 30.0, 30.0)
    registry.register(sleeper)

    connection = await _make_connection(
        connection_service, platform, provider="scripted", configuration={"timeout_seconds": 0.2}
    )
    import time

    validation = ConnectionValidationService(
        db_session,
        registry,
        ProviderExecutor(
            default_policy=RetryPolicy(max_attempts=2, backoff_base_seconds=0.0),
            default_timeout_seconds=0.2,
        ),
        key_box,
    )
    started = time.perf_counter()
    result = await validation.test_connection(connection.id, company_id=platform.company_id)
    elapsed = time.perf_counter() - started

    assert elapsed < 5  # bounded, not 30s
    assert not result.success
    assert result.error_code == "provider_timeout"
    assert result.status_after == ConnectionStatus.ERROR.value
    updated = await connection_service.connections.get(connection.id)
    # 2 attempts were made (transient timeout retried once, idempotent operation).
    assert updated.consecutive_failures == 2


async def test_recovery_from_error_state(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    registry = connection_service.registry
    scripted = ScriptedProvider()
    scripted.queue(ProviderPermanentError("bad config", code="bad_config"))
    registry.register(scripted)

    connection = await _make_connection(
        connection_service, platform, provider="scripted", configuration={"expected_config_key": "x"}
    )
    validation = ConnectionValidationService(
        db_session,
        registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    failed = await validation.test_connection(connection.id, company_id=platform.company_id)
    assert failed.status_after == ConnectionStatus.ERROR.value

    # Fix the configuration and validate again -> recovers to CONNECTED.
    await connection_service.update_configuration(
        connection.id, company_id=platform.company_id, configuration={"expected_config_key": "fixed"}
    )
    recovered = await validation.test_connection(connection.id, company_id=platform.company_id)
    assert recovered.success
    assert recovered.status_after == ConnectionStatus.CONNECTED.value

    updated = await connection_service.connections.get(connection.id)
    assert updated.consecutive_failures == 0
    assert updated.last_error_code is None


async def test_degraded_path_and_recovery(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    registry = connection_service.registry
    scripted = ScriptedProvider()
    scripted.queue({"success": True, "message": "reachable but slow", "metadata": {"degraded": True}})
    registry.register(scripted)

    connection = await _make_connection(
        connection_service, platform, provider="scripted", configuration={"expected_config_key": "x"}
    )
    validation = ConnectionValidationService(
        db_session,
        registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    result = await validation.test_connection(connection.id, company_id=platform.company_id)
    assert result.success
    assert result.status_after == ConnectionStatus.DEGRADED.value
    assert result.degraded

    # Degraded connections recover through a clean validation.
    scripted.queue({"success": True, "message": "all good", "metadata": {}})
    recovered = await validation.test_connection(connection.id, company_id=platform.company_id)
    assert recovered.status_after == ConnectionStatus.CONNECTED.value


async def test_deactivation_and_reactivation(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    connection = await _make_connection(connection_service, platform)

    validation = ConnectionValidationService(
        db_session,
        connection_service.registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    await validation.test_connection(connection.id, company_id=platform.company_id)
    deactivated = await connection_service.deactivate(connection.id, company_id=platform.company_id)
    assert deactivated.status == ConnectionStatus.DISCONNECTED.value

    # A disconnected connection can be validated back to life.
    result = await validation.test_connection(connection.id, company_id=platform.company_id)
    assert result.status_after == ConnectionStatus.CONNECTED.value


async def test_invalid_transition_rejected(connection_service, company_and_platform):
    _, platform = company_and_platform
    connection = await _make_connection(connection_service, platform, configuration={})
    # draft -> disconnected is allowed; draft -> connected is not (must validate first).
    with pytest.raises(ConflictError):
        await connection_service._transition(  # noqa: SLF001 - testing the state machine directly
            connection, ConnectionStatus.CONNECTED, event_type="state_changed"
        )


async def test_validation_writes_audit_events(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    connection = await _make_connection(connection_service, platform)
    validation = ConnectionValidationService(
        db_session,
        connection_service.registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    await validation.test_connection(connection.id, company_id=platform.company_id)

    audits = (
        await db_session.execute(
            select(AuditEvent)
            .where(AuditEvent.resource_type == "connection")
            .order_by(AuditEvent.created_at)
        )
    ).scalars().all()
    types = [event.event_type for event in audits]
    assert "connection.created" in types
    assert "connection.validated" in types


async def test_configuration_update_requires_revalidation(
    db_session, connection_service, key_box, company_and_platform
):
    _, platform = company_and_platform
    connection = await _make_connection(connection_service, platform)

    validation = ConnectionValidationService(
        db_session,
        connection_service.registry,
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )
    await validation.test_connection(connection.id, company_id=platform.company_id)
    connected = await connection_service.connections.get(connection.id)
    assert connected.status == ConnectionStatus.CONNECTED.value

    updated = await connection_service.update_configuration(
        connection.id, company_id=platform.company_id, configuration={"expected_config_key": "new"}
    )
    assert updated.status == ConnectionStatus.CONFIGURED.value
