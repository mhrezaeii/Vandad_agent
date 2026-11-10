"""Phase 1: multi-brand HTTP API — seeding, CRUD, isolation, schema.

These tests exercise the real HTTP surface against PostgreSQL (no provider
calls are involved in brand management).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.models import Company

DEFAULT_BRAND_NAMES = [
    "NobatMoshaver",
    "NobatVakil",
    "VakilBin",
    "Vandefence",
    "Vandiar",
]


@pytest.fixture
def headers(company_and_platform) -> dict:
    company, _platform = company_and_platform
    return {"X-Company-ID": str(company.id)}


@pytest.fixture
async def other_company_headers(db_session) -> dict:
    other = Company(name="Rival Co", slug=f"rival-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.commit()
    return {"X-Company-ID": str(other.id)}


async def _brands(client, headers, **params) -> list[dict]:
    response = await client.get(
        "/api/v1/content/brands", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_list_seeds_five_default_brands_idempotently(client, headers, db_session):
    first = await _brands(client, headers)
    assert [brand["name"] for brand in first] == DEFAULT_BRAND_NAMES
    assert len({brand["id"] for brand in first}) == 5
    assert all(brand["display_name"] for brand in first)
    assert all(brand["cta_config"] == {} for brand in first)

    second = await _brands(client, headers)
    assert [brand["id"] for brand in second] == [brand["id"] for brand in first]

    count = (
        await db_session.execute(text("SELECT count(*) FROM content_brands"))
    ).scalar_one()
    assert count == 5


async def test_seeded_vandefence_profile_is_preserved(client, headers):
    brands = await _brands(client, headers)
    [brand] = [item for item in brands if item["name"] == "Vandefence"]
    assert brand["display_name"] == "Vandefence"
    assert brand["domain"] == "vandefence.ir"
    assert brand["industry"] == "Cybersecurity"
    assert brand["language"] == "fa-IR"
    assert (brand["video_width"], brand["video_height"]) == (1080, 1920)
    assert brand["visual_config"]["background_color"] == "#0A1128"
    assert brand["status"] == "active"


async def test_create_brand_persists_profile(client, headers):
    payload = {
        "name": "NewBrand",
        "display_name": "New Brand",
        "tone": "صمیمی و روشن",
        "language": "fa-IR",
        "description": "یک برند آزمایشی",
        "cta_config": {
            "text": "همین حالا شروع کنید",
            "label": "شروع",
            "url": "https://example.com/start",
            "hashtags": ["#test"],
        },
        "domain": "example.com",
        "industry": "Services",
    }
    response = await client.post(
        "/api/v1/content/brands", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "NewBrand"
    assert body["display_name"] == "New Brand"
    assert body["tone"] == payload["tone"]
    assert body["description"] == payload["description"]
    assert body["cta_config"] == payload["cta_config"]
    assert body["domain"] == "example.com"
    assert body["industry"] == "Services"
    assert body["status"] == "active"
    assert (body["video_width"], body["video_height"]) == (1080, 1920)

    fetched = await client.get(
        f"/api/v1/content/brands/{body['id']}", headers=headers
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == body


async def test_create_brand_rejects_duplicates(client, headers):
    await _brands(client, headers)  # seed defaults

    seeded = await client.post(
        "/api/v1/content/brands", json={"name": "Vandefence"}, headers=headers
    )
    assert seeded.status_code == 409

    created = await client.post(
        "/api/v1/content/brands", json={"name": "UniqueBrand"}, headers=headers
    )
    assert created.status_code == 201, created.text

    exact = await client.post(
        "/api/v1/content/brands", json={"name": "UniqueBrand"}, headers=headers
    )
    assert exact.status_code == 409

    # Whitespace/case variants are the same brand key.
    variant = await client.post(
        "/api/v1/content/brands", json={"name": "  uniquebrand  "}, headers=headers
    )
    assert variant.status_code == 409


async def test_same_brand_name_allowed_across_companies(
    client, headers, other_company_headers
):
    created_a = await client.post(
        "/api/v1/content/brands", json={"name": "Shared"}, headers=headers
    )
    created_b = await client.post(
        "/api/v1/content/brands", json={"name": "Shared"}, headers=other_company_headers
    )
    assert created_a.status_code == 201, created_a.text
    assert created_b.status_code == 201, created_b.text
    assert created_a.json()["id"] != created_b.json()["id"]


async def test_patch_brand_updates_fields_and_archives(client, headers):
    brands = await _brands(client, headers)
    [brand] = [item for item in brands if item["name"] == "Vandiar"]

    response = await client.patch(
        f"/api/v1/content/brands/{brand['id']}",
        json={
            "display_name": "وندیار",
            "tone": "لحن تازه",
            "language": "fa-IR",
            "description": "برند وندیار",
            "cta_config": {"text": "شروع کنید", "hashtags": ["#vandiar"]},
            "status": "archived",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["display_name"] == "وندیار"
    assert body["tone"] == "لحن تازه"
    assert body["description"] == "برند وندیار"
    assert body["cta_config"] == {"text": "شروع کنید", "hashtags": ["#vandiar"]}
    assert body["status"] == "archived"

    # Archived brands leave the default list but remain retrievable and are
    # never re-seeded as duplicates.
    active_names = [item["name"] for item in await _brands(client, headers)]
    assert "Vandiar" not in active_names

    all_brands = await _brands(client, headers, include_archived="true")
    vandiar = [item for item in all_brands if item["name"] == "Vandiar"]
    assert len(vandiar) == 1
    assert vandiar[0]["id"] == brand["id"]


async def test_patch_can_clear_nullable_fields_and_keeps_name_stable(client, headers):
    brands = await _brands(client, headers)
    [brand] = [item for item in brands if item["name"] == "NobatVakil"]

    response = await client.patch(
        f"/api/v1/content/brands/{brand['id']}",
        json={"description": None, "display_name": "نوبت وکیل"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["description"] is None
    assert response.json()["display_name"] == "نوبت وکیل"
    assert response.json()["name"] == "NobatVakil"

    # Empty patch is a no-op.
    noop = await client.patch(
        f"/api/v1/content/brands/{brand['id']}", json={}, headers=headers
    )
    assert noop.status_code == 200, noop.text
    assert noop.json()["display_name"] == "نوبت وکیل"

    # ``name`` is the stable seeding key and cannot be renamed via PATCH.
    renamed = await client.patch(
        f"/api/v1/content/brands/{brand['id']}",
        json={"name": "RenamedBrand"},
        headers=headers,
    )
    assert renamed.status_code == 422


async def test_patch_rejects_null_for_non_nullable_fields(client, headers):
    brands = await _brands(client, headers)
    [brand] = [item for item in brands if item["name"] == "NobatMoshaver"]

    response = await client.patch(
        f"/api/v1/content/brands/{brand['id']}",
        json={"language": None},
        headers=headers,
    )
    assert response.status_code == 422

    response = await client.patch(
        f"/api/v1/content/brands/{brand['id']}",
        json={"status": None},
        headers=headers,
    )
    assert response.status_code == 422


async def test_brand_payload_validation(client, headers):
    blank = await client.post(
        "/api/v1/content/brands", json={"name": "   "}, headers=headers
    )
    assert blank.status_code == 422

    unknown = await client.post(
        "/api/v1/content/brands",
        json={"name": "BrandX", "not_a_field": 1},
        headers=headers,
    )
    assert unknown.status_code == 422

    bad_language = await client.post(
        "/api/v1/content/brands",
        json={"name": "BrandY", "language": "x"},
        headers=headers,
    )
    assert bad_language.status_code == 422

    bad_cta = await client.post(
        "/api/v1/content/brands",
        json={"name": "BrandZ", "cta_config": {"unexpected": "value"}},
        headers=headers,
    )
    assert bad_cta.status_code == 422


async def test_unknown_brand_is_404(client, headers):
    unknown = uuid.uuid4()
    response = await client.get(
        f"/api/v1/content/brands/{unknown}", headers=headers
    )
    assert response.status_code == 404
    patched = await client.patch(
        f"/api/v1/content/brands/{unknown}",
        json={"display_name": "x"},
        headers=headers,
    )
    assert patched.status_code == 404


async def test_brands_are_isolated_between_companies(
    client, headers, other_company_headers
):
    brands_a = await _brands(client, headers)
    target = brands_a[0]

    foreign_get = await client.get(
        f"/api/v1/content/brands/{target['id']}", headers=other_company_headers
    )
    assert foreign_get.status_code == 404

    foreign_patch = await client.patch(
        f"/api/v1/content/brands/{target['id']}",
        json={"display_name": "hijacked"},
        headers=other_company_headers,
    )
    assert foreign_patch.status_code == 404

    brands_b = await _brands(client, other_company_headers)
    assert [item["name"] for item in brands_b] == DEFAULT_BRAND_NAMES
    assert {item["id"] for item in brands_b}.isdisjoint(
        {item["id"] for item in brands_a}
    )

    # The failed cross-tenant patch changed nothing.
    unchanged = await client.get(
        f"/api/v1/content/brands/{target['id']}", headers=headers
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["display_name"] == target["display_name"]


async def test_brand_profile_columns_migration(db_session):
    rows = (
        await db_session.execute(
            text(
                "SELECT column_name, is_nullable, data_type "
                "FROM information_schema.columns "
                "WHERE table_name = 'content_brands' "
                "AND column_name IN ('display_name', 'description', 'cta_config')"
            )
        )
    ).all()
    columns = {row.column_name: row for row in rows}
    assert set(columns) == {"display_name", "description", "cta_config"}
    assert columns["display_name"].is_nullable == "YES"
    assert columns["display_name"].data_type == "character varying"
    assert columns["description"].is_nullable == "YES"
    assert columns["description"].data_type == "text"
    assert columns["cta_config"].is_nullable == "NO"
    assert columns["cta_config"].data_type == "jsonb"
