"""Brand-to-platform knowledge wiring tests."""
from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import NotFoundError
from app.models import Company, ContentBrand, Platform
from app.services.content_service import ContentService


@pytest.fixture
def service(db_session, engine_pair, key_box):
    registry, executor = engine_pair
    return ContentService(db_session, registry, executor, key_box)


async def test_brand_platform_links_are_tenant_scoped_and_replaceable(
    db_session, service, company_and_platform
):
    company, platform = company_and_platform
    brand = await service.create_brand(
        company_id=company.id,
        data={"name": f"Brand-{uuid.uuid4().hex[:8]}", "language": "fa-IR"},
    )
    linked = await service.set_brand_platforms(
        brand.id, company_id=company.id, platform_ids=[platform.id, platform.id]
    )
    assert linked == [platform.id]
    rows = await service.list_brand_platforms(brand.id, company_id=company.id)
    assert [row.id for row in rows] == [platform.id]

    await service.set_brand_platforms(
        brand.id, company_id=company.id, platform_ids=[]
    )
    assert await service.list_brand_platforms(brand.id, company_id=company.id) == []


async def test_brand_platform_link_rejects_cross_tenant_platform(
    db_session, service, company_and_platform
):
    company, _platform = company_and_platform
    other_company = Company(
        name="Other Tenant",
        slug=f"other-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(other_company)
    await db_session.flush()
    other_platform = Platform(
        company_id=other_company.id,
        name="Other Platform",
        slug=f"other-platform-{uuid.uuid4().hex[:8]}",
        base_url="https://other.example",
    )
    brand = await service.create_brand(
        company_id=company.id,
        data={"name": f"Brand-{uuid.uuid4().hex[:8]}", "language": "fa-IR"},
    )
    db_session.add(other_platform)
    await db_session.flush()

    with pytest.raises(NotFoundError):
        await service.set_brand_platforms(
            brand.id, company_id=company.id, platform_ids=[other_platform.id]
        )
