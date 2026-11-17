"""Multi-tenancy tests: company isolation for connections and credentials."""

from __future__ import annotations

import base64
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.integrations.providers import ProviderExecutor, RetryPolicy, build_default_registry
from app.models import Company, Platform
from app.security.authorization import (
    Principal,
    authorize_company_access,
    build_principal_from_settings,
)
from app.security.crypto import MasterKeyBox
from app.services.connection_service import ConnectionService
from app.services.connection_validation_service import ConnectionValidationService
from app.services.credential_service import CredentialService
from tests.conftest import TEST_MASTER_KEY_CONFIG


@pytest.fixture
def key_box() -> MasterKeyBox:
    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


@pytest.fixture
async def two_companies_with_platforms(db_session: AsyncSession):
    company_a = Company(name="Alpha Co", slug="alpha-co")
    company_b = Company(name="Beta Co", slug="beta-co")
    db_session.add_all([company_a, company_b])
    await db_session.flush()
    platform_a = Platform(company_id=company_a.id, name="Alpha Admin", slug="alpha-admin")
    platform_b = Platform(company_id=company_b.id, name="Beta Admin", slug="beta-admin")
    db_session.add_all([platform_a, platform_b])
    await db_session.commit()
    return (company_a, platform_a), (company_b, platform_b)


def _service(db_session, key_box):
    return ConnectionService(db_session, build_default_registry(), master_key_box=key_box)


def _validation(db_session, key_box):
    return ConnectionValidationService(
        db_session,
        build_default_registry(),
        ProviderExecutor(default_policy=RetryPolicy(max_attempts=1), default_timeout_seconds=2.0),
        key_box,
    )


async def test_connection_of_other_company_reads_as_missing(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, _) = two_companies_with_platforms
    service = _service(db_session, key_box)
    connection = await service.create_connection(
        company_id=company_a.id,
        platform_id=platform_a.id,
        name="alpha-conn",
        provider="diagnostic",
        configuration={"expected_config_key": "x"},
    )

    with pytest.raises(NotFoundError):
        await service.get_connection(connection.id, company_id=company_b.id)


async def test_connection_of_other_company_cannot_be_listed(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, _) = two_companies_with_platforms
    service = _service(db_session, key_box)
    await service.create_connection(
        company_id=company_a.id,
        platform_id=platform_a.id,
        name="alpha-conn",
        provider="diagnostic",
        configuration={"expected_config_key": "x"},
    )

    items, total = await service.list_connections(company_id=company_b.id)
    assert total == 0
    assert items == []


async def test_connection_cannot_be_created_for_foreign_platform(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, _) = two_companies_with_platforms
    service = _service(db_session, key_box)
    with pytest.raises(NotFoundError):  # existence of B's platform is not revealed
        await service.create_connection(
            company_id=company_b.id,
            platform_id=platform_a.id,
            name="hijack",
            provider="diagnostic",
            configuration={"expected_config_key": "x"},
        )


async def test_connection_of_other_company_cannot_be_tested(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, _) = two_companies_with_platforms
    service = _service(db_session, key_box)
    connection = await service.create_connection(
        company_id=company_a.id,
        platform_id=platform_a.id,
        name="alpha-conn",
        provider="diagnostic",
        configuration={"expected_config_key": "x"},
    )

    with pytest.raises(NotFoundError):
        await _validation(db_session, key_box).test_connection(
            connection.id, company_id=company_b.id
        )


async def test_credential_of_other_company_cannot_be_resolved(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, _) = two_companies_with_platforms
    credential_service = CredentialService(db_session, key_box)
    reference = await credential_service.create_credential(
        platform_id=platform_a.id,
        credential_type="api_key",
        secret_reference="vault://alpha/api-key",
        storage_type="encrypted",
        value=__import__("app").security.secret_value.SecretValue("alpha-secret"),
    )

    with pytest.raises(ForbiddenError):
        await credential_service.resolve_credential(reference.id, company_id=company_b.id)


async def test_credential_cannot_be_attached_to_foreign_connection(
    db_session, key_box, two_companies_with_platforms
):
    (company_a, platform_a), (company_b, platform_b) = two_companies_with_platforms
    credential_service = CredentialService(db_session, key_box)
    reference = await credential_service.create_credential(
        platform_id=platform_a.id,
        credential_type="api_key",
        secret_reference="vault://alpha/api-key",
        storage_type="reference",
    )

    service = _service(db_session, key_box)
    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        await service.create_connection(
            company_id=company_b.id,
            platform_id=platform_b.id,
            name="beta-conn",
            provider="diagnostic",
            configuration={"expected_config_key": "x"},
            credential_reference_id=reference.id,
        )


async def test_composite_fk_blocks_cross_tenant_platform_link(db_session, key_box):
    """The database itself must refuse (platform_id, company_id) mismatches."""
    from sqlalchemy.exc import IntegrityError

    from app.models import PlatformConnection

    company_a = Company(name="Gamma Co", slug="gamma-co")
    db_session.add(company_a)
    await db_session.flush()
    platform_a = Platform(company_id=company_a.id, name="Gamma Admin", slug="gamma-admin")
    db_session.add(platform_a)
    await db_session.commit()

    stranger = uuid.uuid4()
    db_session.add(
        PlatformConnection(
            company_id=stranger,
            platform_id=platform_a.id,
            name="forged",
            provider="diagnostic",
            configuration={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


def test_principal_authorization_boundary():
    company_a = uuid.uuid4()
    company_b = uuid.uuid4()
    principal = Principal(subject="ops", company_ids=frozenset({str(company_a)}))

    scope = authorize_company_access(principal, company_a)
    assert scope.company_id == company_a

    from app.core.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError):
        authorize_company_access(principal, company_b)


def test_wildcard_principal_grants_all(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setattr("app.core.config.Settings.is_production", False)
    settings = Settings(bootstrap_principal_company_ids="*", _env_file=None)
    principal = build_principal_from_settings(settings)
    assert principal.may_access_company(uuid.uuid4())


def test_wildcard_principal_refused_in_production():
    from app.core.config import Settings

    settings = Settings(environment="production", bootstrap_principal_company_ids="*", _env_file=None)
    with pytest.raises(Exception):  # noqa: B017 - ForbiddenError subclass
        build_principal_from_settings(settings)


def test_explicit_company_list_principal():
    from app.core.config import Settings

    company_a = str(uuid.uuid4())
    settings = Settings(bootstrap_principal_company_ids=company_a, _env_file=None)
    principal = build_principal_from_settings(settings)
    assert principal.may_access_company(uuid.UUID(company_a))
    assert not principal.may_access_company(uuid.uuid4())
