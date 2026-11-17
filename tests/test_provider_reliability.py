"""Reliability tests: timeouts, retry classification, correlation propagation."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy import select

from app.core.correlation import set_correlation_id
from app.integrations.providers import (
    ProviderExecutor,
    ProviderTransientError,
    RetryPolicy,
    build_default_registry,
)
from app.models import ConnectionEvent, ConnectionStatus
from app.security.crypto import MasterKeyBox
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
from tests.conftest import TEST_MASTER_KEY_CONFIG
from tests.provider_mocks import (
    AlwaysPermanentProvider,
    AlwaysTransientProvider,
    ScriptedProvider,
)


@pytest.fixture
def key_box() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


@pytest.fixture
def service(db_session, key_box):
    return ConnectionService(db_session, build_default_registry(), master_key_box=key_box)


async def _connection(service, platform, provider="diagnostic", configuration=None):
    return await service.create_connection(
        company_id=platform.company_id,
        platform_id=platform.id,
        name=f"conn-{uuid.uuid4().hex[:8]}",
        provider=provider,
        configuration=configuration if configuration is not None else {"expected_config_key": "x"},
    )


def _validation(db_session, service, key_box, policy):
    return ConnectionValidationService(
        db_session,
        service.registry,
        ProviderExecutor(default_policy=policy, default_timeout_seconds=2.0),
        key_box,
    )


async def test_transient_failures_are_retried_until_success(
    db_session, service, key_box, company_and_platform, fast_retry_policy
):
    _, platform = company_and_platform
    flaky = ScriptedProvider()
    flaky.queue(ProviderTransientError("blip 1", code="blip"))
    flaky.queue(ProviderTransientError("blip 2", code="blip"))
    service.registry.register(flaky)

    connection = await _connection(service, platform, "scripted")
    result = await _validation(db_session, service, key_box, fast_retry_policy).test_connection(
        connection.id, company_id=platform.company_id
    )

    assert result.success
    assert result.attempts == 3
    assert flaky.calls == 3


async def test_permanent_failures_are_never_retried(
    db_session, service, key_box, company_and_platform, fast_retry_policy
):
    _, platform = company_and_platform
    broken = AlwaysPermanentProvider()
    service.registry.register(broken)

    connection = await _connection(service, platform, "always_permanent")
    result = await _validation(db_session, service, key_box, fast_retry_policy).test_connection(
        connection.id, company_id=platform.company_id
    )

    assert not result.success
    assert result.attempts == 1  # no retry for permanent failures
    assert broken.calls == 1
    assert result.error_code == "bad_config"


async def test_transient_exhaustion_records_failure(
    db_session, service, key_box, company_and_platform, fast_retry_policy
):
    _, platform = company_and_platform
    failing = AlwaysTransientProvider()
    service.registry.register(failing)

    connection = await _connection(service, platform, "always_transient")
    result = await _validation(db_session, service, key_box, fast_retry_policy).test_connection(
        connection.id, company_id=platform.company_id
    )

    assert not result.success
    assert result.attempts == 3
    assert failing.calls == 3
    updated = await service.connections.get(connection.id)
    assert updated.consecutive_failures == 3
    assert updated.status == ConnectionStatus.ERROR.value


async def test_timeout_never_blocks_beyond_budget(
    db_session, service, key_box, company_and_platform, fast_retry_policy
):
    import time

    _, platform = company_and_platform
    sleeper = ScriptedProvider()
    sleeper.queue(60.0, 60.0, 60.0)  # every attempt exceeds the budget
    service.registry.register(sleeper)

    connection = await _connection(
        service, platform, "scripted", configuration={"timeout_seconds": 0.2}
    )
    started = time.perf_counter()
    result = await _validation(db_session, service, key_box, fast_retry_policy).test_connection(
        connection.id, company_id=platform.company_id
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 5  # budget 0.2s x 3 attempts + tiny backoff, far below 60s
    assert result.error_code == "provider_timeout"


async def test_executor_classifies_unknown_errors_as_permanent(key_box):
    """Unexpected exceptions become sanitized permanent failures."""
    executor = ProviderExecutor(
        default_policy=RetryPolicy(max_attempts=3, backoff_base_seconds=0.0), default_timeout_seconds=1.0
    )

    class BoomAdapter(ScriptedProvider):
        async def validate_connection(self, context):
            raise RuntimeError("internal provider crash: password=hunter2")

    adapter = BoomAdapter()
    context = executor.build_context(
        adapter=adapter,
        connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        platform_id=uuid.uuid4(),
        configuration={},
        credential=None,
        credential_type=None,
        secret_reference=None,
        timeout_seconds=1.0,
    )
    outcome = await executor.run(adapter, context, adapter.validate_connection, idempotent=True)
    assert not outcome.success
    assert outcome.attempts == 1  # unknown -> permanent -> no retry
    # Sanitized: no exception text, no secret fragments.
    assert "crash" not in outcome.error.message
    assert "hunter2" not in outcome.error.message
    assert outcome.error.code == "provider_failure"


async def test_non_idempotent_operations_never_retry(key_box):
    """Side-effectful operations get exactly one attempt even on transient errors."""
    executor = ProviderExecutor(
        default_policy=RetryPolicy(max_attempts=5, backoff_base_seconds=0.0), default_timeout_seconds=1.0
    )
    adapter = AlwaysTransientProvider()
    context = executor.build_context(
        adapter=adapter,
        connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        platform_id=uuid.uuid4(),
        configuration={"idempotent": True},
        credential=None,
        credential_type=None,
        secret_reference=None,
        operation="execute",
    )
    outcome = await executor.run(
        adapter, context, adapter.validate_connection, idempotent=False
    )
    assert outcome.attempts == 1
    assert adapter.calls == 1


async def test_correlation_id_propagates_into_events(
    db_session, service, key_box, company_and_platform
):
    _, platform = company_and_platform
    connection = await _connection(service, platform)
    correlation_id = "corr-propagation-12345678"

    token = set_correlation_id(correlation_id)
    try:
        validation = _validation(
            db_session, service, key_box, RetryPolicy(max_attempts=1, backoff_base_seconds=0.0)
        )
        await validation.test_connection(connection.id, company_id=platform.company_id)
    finally:
        from app.core.correlation import reset_correlation_id

        reset_correlation_id(token)

    events = (
        await db_session.execute(
            select(ConnectionEvent).where(ConnectionEvent.connection_id == connection.id)
        )
    ).scalars().all()
    assert events
    # Events produced inside the correlation window must carry the id (the
    # "created" event predates it in this service-level test).
    validation_events = [event for event in events if event.event_type != "created"]
    assert validation_events
    assert all(event.correlation_id == correlation_id for event in validation_events)


async def test_leaky_provider_message_is_redacted(
    db_session, service, key_box, company_and_platform, fast_retry_policy
):
    """A misbehaving provider echoing secret material must not store it verbatim."""
    from tests.provider_mocks import SecretEchoProvider

    _, platform = company_and_platform
    leaky = SecretEchoProvider()
    service.registry.register(leaky)

    from app.models import CredentialStorageType
    from app.security.secret_value import SecretValue
    from app.services.credential_service import CredentialService as CS

    cs = CS(db_session, key_box)
    reference = await cs.create_credential(
        platform_id=platform.id,
        credential_type="api_key",
        secret_reference="vault://acme/leaky",
        storage_type=CredentialStorageType.ENCRYPTED,
        value=SecretValue("echoed-plain-secret"),
    )
    connection = await service.create_connection(
        company_id=platform.company_id,
        platform_id=platform.id,
        name="leaky-conn",
        provider="secret_echo",
        configuration={"expected_config_key": "x"},
        credential_reference_id=reference.id,
    )

    result = await _validation(db_session, service, key_box, fast_retry_policy).test_connection(
        connection.id, company_id=platform.company_id
    )
    assert not result.success
    assert "echoed-plain-secret" not in result.message

    updated = await service.connections.get(connection.id)
    assert updated.last_error_message is not None
    assert "echoed-plain-secret" not in updated.last_error_message
    assert "***REDACTED***" in updated.last_error_message
