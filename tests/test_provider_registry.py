"""Provider architecture tests: registry, contract, capabilities."""

from __future__ import annotations

import pytest

from app.integrations.providers import (
    DiagnosticProvider,
    DuplicateProviderError,
    HttpEndpointProvider,
    ProviderAdapter,
    ProviderCapability,
    ProviderRegistry,
    UnknownProviderError,
    build_default_registry,
)
from tests.provider_mocks import ScriptedProvider


def test_build_default_registry_contains_builtins():
    registry = build_default_registry()
    ids = [adapter.provider_id for adapter in registry.list_adapters()]
    assert "diagnostic" in ids
    assert "http_endpoint" in ids
    assert ids == sorted(ids)  # deterministic listing


def test_register_and_get():
    registry = ProviderRegistry()
    adapter = ScriptedProvider()
    registry.register(adapter)
    assert registry.get("scripted") is adapter
    assert registry.has("scripted")
    assert len(registry) == 1


def test_duplicate_registration_rejected():
    registry = ProviderRegistry()
    registry.register(ScriptedProvider())
    with pytest.raises(DuplicateProviderError):
        registry.register(ScriptedProvider())


def test_missing_provider_raises_unknown():
    registry = ProviderRegistry()
    with pytest.raises(UnknownProviderError) as excinfo:
        registry.get("does-not-exist")
    assert excinfo.value.provider_id == "does-not-exist"


def test_adapter_must_define_provider_id():
    with pytest.raises(TypeError, match="provider_id"):

        class NoIdAdapter(ProviderAdapter):
            async def validate_connection(self, context):
                pass  # pragma: no cover


def test_adapter_is_abstract():
    with pytest.raises(TypeError):
        ProviderAdapter()  # type: ignore[abstract]


def test_diagnostic_provider_declares_validation_capability():
    adapter = DiagnosticProvider()
    assert ProviderCapability.CONNECTION_VALIDATION in adapter.capabilities
    assert adapter.provider_id == "diagnostic"


def test_http_provider_requires_url_configuration_shape():
    adapter = HttpEndpointProvider()
    requirement_types = {req.credential_type for req in adapter.credential_requirements}
    assert requirement_types <= {"api_key", "token"}
    assert adapter.provider_id == "http_endpoint"


async def test_diagnostic_provider_validates_configuration():
    adapter = DiagnosticProvider()
    context = _context(configuration={"expected_config_key": "value"})
    result = await adapter.validate_connection(context)
    assert result.success

    from app.integrations.providers.base import ProviderPermanentError

    bad_context = _context(configuration={})
    with pytest.raises(ProviderPermanentError):
        await adapter.validate_connection(bad_context)


def _context(configuration):
    import uuid

    from app.integrations.providers.base import ProviderExecutionContext

    return ProviderExecutionContext(
        provider_id="diagnostic",
        connection_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        platform_id=uuid.uuid4(),
        configuration=configuration,
        credential=None,
        credential_type=None,
        secret_reference=None,
        timeout_seconds=2.0,
    )
