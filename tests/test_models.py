"""Database model tests: relationships, constraints, timestamps, audit trail."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    ActorType,
    AuditEvent,
    Company,
    CredentialReference,
    Platform,
    WorkflowExecution,
    WorkflowStatus,
    WorkflowType,
)


async def _commit(session: AsyncSession) -> None:
    await session.commit()


async def test_company_platform_relationship(db_session: AsyncSession):
    company = Company(name="Acme", slug="acme")
    db_session.add(company)
    await db_session.flush()
    platform = Platform(company_id=company.id, name="Admin", slug="admin", platform_type="admin_panel")
    db_session.add(platform)
    await _commit(db_session)

    loaded = (
        await db_session.execute(
            select(Company).options(selectinload(Company.platforms)).where(Company.slug == "acme")
        )
    ).scalar_one()
    assert len(loaded.platforms) == 1
    assert loaded.platforms[0].name == "Admin"


async def test_platform_credential_relationship_and_restrict(db_session: AsyncSession):
    company = Company(name="Acme", slug="acme")
    db_session.add(company)
    await db_session.flush()
    platform = Platform(company_id=company.id, name="Admin", slug="admin")
    db_session.add(platform)
    await db_session.flush()
    db_session.add_all(
        [
            CredentialReference(platform_id=platform.id, credential_type="api_key", secret_reference="vault://admin/key"),
            CredentialReference(platform_id=platform.id, credential_type="oauth2", secret_reference="vault://admin/oauth"),
        ]
    )
    await _commit(db_session)

    loaded = (
        await db_session.execute(
            select(Platform).options(selectinload(Platform.credentials)).where(Platform.slug == "admin")
        )
    ).scalar_one()
    platform_id = loaded.id  # capture before any expiry
    assert len(loaded.credentials) == 2
    assert loaded.credentials[0].credential_type == "api_key"

    # ON DELETE RESTRICT: a platform with credential references cannot be
    # deleted until they are removed explicitly.
    await db_session.delete(loaded)
    with pytest.raises(IntegrityError):
        await _commit(db_session)
    await db_session.rollback()

    # Removing the credentials first allows the deletion.
    await db_session.execute(
        CredentialReference.__table__.delete().where(CredentialReference.platform_id == platform_id)
    )
    await db_session.delete(loaded)
    await _commit(db_session)
    remaining = (
        await db_session.execute(select(func.count()).select_from(Platform))
    ).scalar_one()
    assert remaining == 0


async def test_company_delete_cascades_platforms_and_preserves_audit(
    db_session: AsyncSession,
):
    company = Company(name="Acme", slug="acme")
    db_session.add(company)
    await db_session.flush()
    platform = Platform(company_id=company.id, name="Admin", slug="admin")
    db_session.add(platform)
    await db_session.flush()
    audit = AuditEvent(
        event_type="platform.created",
        action="create",
        company_id=company.id,
        platform_id=platform.id,
        resource_type="platform",
        resource_id=str(platform.id),
    )
    db_session.add(audit)
    await _commit(db_session)

    await db_session.delete(company)
    await _commit(db_session)

    # Refresh the identity map so DB-side SET NULL changes are visible.
    db_session.expire_all()

    assert (await db_session.execute(select(func.count()).select_from(Platform))).scalar_one() == 0
    audit_after = (await db_session.execute(select(AuditEvent))).scalar_one()
    assert audit_after.id == audit.id  # history preserved
    assert audit_after.company_id is None  # reference cleared, not cascaded away
    assert audit_after.platform_id is None


async def test_platform_slug_unique_within_company_only(db_session: AsyncSession):
    company_a = Company(name="Alpha", slug="alpha")
    company_b = Company(name="Beta", slug="beta")
    db_session.add_all([company_a, company_b])
    await db_session.flush()
    db_session.add(Platform(company_id=company_a.id, name="Admin", slug="admin"))
    await _commit(db_session)

    # Different company, same slug: allowed.
    db_session.add(Platform(company_id=company_b.id, name="Admin", slug="admin"))
    await _commit(db_session)

    # Same company, same slug: rejected.
    db_session.add(Platform(company_id=company_a.id, name="Duplicate", slug="admin"))
    with pytest.raises(IntegrityError):
        await _commit(db_session)
    await db_session.rollback()


async def test_platform_type_check_constraint(db_session: AsyncSession):
    company = Company(name="Acme", slug="acme")
    db_session.add(company)
    await db_session.flush()
    db_session.add(Platform(company_id=company.id, name="Bad", slug="bad", platform_type="not-a-type"))
    with pytest.raises(IntegrityError):
        await _commit(db_session)
    await db_session.rollback()


async def test_workflow_execution_lifecycle_fields(db_session: AsyncSession):
    now = datetime.now(UTC)
    execution = WorkflowExecution(
        workflow_name="publish-marketing-post",
        workflow_type=WorkflowType.SOCIAL_PUBLISHING.value,
        status=WorkflowStatus.SUCCEEDED.value,
        execution_reference="n8n-exec-12345",
        input_metadata={"channel": "linkedin", "post_id": "abc"},
        output_metadata={"published": True},
        started_at=now - timedelta(minutes=5),
        completed_at=now,
    )
    db_session.add(execution)
    await _commit(db_session)

    loaded = (await db_session.execute(select(WorkflowExecution))).scalar_one()
    assert loaded.workflow_name == "publish-marketing-post"
    assert loaded.status == "succeeded"
    assert loaded.input_metadata == {"channel": "linkedin", "post_id": "abc"}
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_workflow_execution_rejects_completed_before_started(db_session: AsyncSession):
    now = datetime.now(UTC)
    db_session.add(
        WorkflowExecution(
            workflow_name="broken",
            started_at=now,
            completed_at=now - timedelta(minutes=1),
        )
    )
    with pytest.raises(IntegrityError):
        await _commit(db_session)
    await db_session.rollback()


async def test_workflow_execution_defaults(db_session: AsyncSession):
    db_session.add(WorkflowExecution(workflow_name="default-check"))
    await _commit(db_session)
    loaded = (await db_session.execute(select(WorkflowExecution))).scalar_one()
    assert loaded.status == WorkflowStatus.PENDING.value
    assert loaded.workflow_type == WorkflowType.CUSTOM.value
    assert loaded.completed_at is None


async def test_audit_event_fields_and_metadata(db_session: AsyncSession):
    db_session.add(
        AuditEvent(
            event_type="platform.updated",
            action="update",
            actor_type=ActorType.AGENT.value,
            actor_id="marketing-agent-01",
            resource_type="platform",
            resource_id=str(uuid.uuid4()),
            event_metadata={"changed_fields": ["status"]},
        )
    )
    await _commit(db_session)

    loaded = (await db_session.execute(select(AuditEvent))).scalar_one()
    assert loaded.actor_type == "agent"
    assert loaded.actor_id == "marketing-agent-01"
    assert loaded.event_metadata == {"changed_fields": ["status"]}
    # The DB column is named "metadata" as per the schema contract.
    assert "metadata" in AuditEvent.__table__.columns


async def test_timestamps_are_utc_and_auto_set(db_session: AsyncSession):
    company = Company(name="Acme", slug="acme")
    db_session.add(company)
    await _commit(db_session)

    loaded = (await db_session.execute(select(Company))).scalar_one()
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at.tzinfo is not None
    assert loaded.created_at <= datetime.now(UTC) + timedelta(seconds=1)
