"""Phase 1: brand service tests — seeding, duplicate prevention, isolation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from app.models import Company, ContentBrand
from app.services.content_service import (
    DEFAULT_BRAND_NAMES,
    ContentService,
)


@pytest.fixture
def service(db_session, engine_pair, key_box) -> ContentService:
    registry, executor = engine_pair
    return ContentService(db_session, registry, executor, key_box)


@pytest.fixture
async def company(db_session) -> Company:
    company = Company(name="Brand Co", slug=f"brand-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.commit()
    return company


async def _brand_count(db_session, company_id) -> int:
    return (
        await db_session.execute(
            select(func.count())
            .select_from(ContentBrand)
            .where(ContentBrand.company_id == company_id)
        )
    ).scalar_one()


async def test_ensure_default_brands_is_idempotent(service, db_session, company):
    first = await service.ensure_default_brands(company_id=company.id)
    await db_session.commit()
    assert [brand.name for brand in first] == list(DEFAULT_BRAND_NAMES)

    second = await service.ensure_default_brands(company_id=company.id)
    await db_session.commit()
    assert [brand.id for brand in second] == [brand.id for brand in first]
    assert await _brand_count(db_session, company.id) == 5


async def test_ensure_vandefence_brand_is_compatible_with_seeding(
    service, db_session, company
):
    seeded = await service.ensure_default_brands(company_id=company.id)
    await db_session.commit()
    [vandefence] = [brand for brand in seeded if brand.name == "Vandefence"]

    brand = await service.ensure_vandefence_brand(company_id=company.id)
    await db_session.commit()
    assert brand.id == vandefence.id
    assert brand.domain == "vandefence.ir"
    assert await _brand_count(db_session, company.id) == 5


async def test_create_brand_rejects_case_insensitive_duplicate(
    service, db_session, company
):
    await service.create_brand(
        company_id=company.id,
        data={"name": "Alpha", "display_name": "Alpha", "language": "fa-IR"},
    )
    await db_session.commit()

    with pytest.raises(ConflictError):
        await service.create_brand(
            company_id=company.id,
            data={"name": "ALPHA", "language": "fa-IR"},
        )


async def test_create_brand_allows_same_name_for_other_company(
    service, db_session, company
):
    other = Company(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.commit()

    first = await service.create_brand(
        company_id=company.id, data={"name": "Shared", "language": "fa-IR"}
    )
    second = await service.create_brand(
        company_id=other.id, data={"name": "Shared", "language": "fa-IR"}
    )
    await db_session.commit()
    assert first.id != second.id


async def test_update_brand_archive_is_reflected_in_listing(
    service, db_session, company
):
    seeded = await service.ensure_default_brands(company_id=company.id)
    await db_session.commit()
    [target] = [brand for brand in seeded if brand.name == "VakilBin"]

    updated = await service.update_brand(
        target.id, company_id=company.id, changes={"status": "archived"}
    )
    await db_session.commit()
    assert updated.status == "archived"

    active = await service.list_brands(company_id=company.id)
    assert "VakilBin" not in [brand.name for brand in active]

    all_brands = await service.list_brands(
        company_id=company.id, include_archived=True
    )
    assert "VakilBin" in [brand.name for brand in all_brands]
    assert await _brand_count(db_session, company.id) == 5


async def test_update_brand_rejects_null_language(service, db_session, company):
    brands = await service.ensure_default_brands(company_id=company.id)
    await db_session.commit()
    brand = brands[0]

    with pytest.raises(UnprocessableEntityError):
        await service.update_brand(
            brand.id, company_id=company.id, changes={"language": None}
        )


async def test_update_brand_isolated_by_company(service, db_session, company):
    other = Company(name="Other Co", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.commit()

    [brand] = [
        item
        for item in await service.ensure_default_brands(company_id=company.id)
        if item.name == "Vandefence"
    ]
    await db_session.commit()

    with pytest.raises(NotFoundError):
        await service.update_brand(
            brand.id,
            company_id=other.id,
            changes={"display_name": "hijacked"},
        )
