"""Resolve and link the public monitoring source for a content brand.

The production agent and the brand-memory refresh endpoint both need the same
behaviour: if the brand already has a monitored platform, reuse it; otherwise
create (or reuse) a public website platform from the operator-supplied URL and
link it to the brand. Keeping this in one module means the two entry points can
never drift apart.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.slug import slugify
from app.core.exceptions import ConflictError, UnprocessableEntityError
from app.integrations.platform_discovery.public_sources import (
    normalise_url,
    validate_public_url,
)
from app.models import IntegrationType, Platform, PlatformStatus, PlatformType
from app.schemas.platform import PlatformCreate
from app.services.platform_service import PlatformService


async def ensure_brand_source(
    session: AsyncSession,
    service,  # noqa: ANN001 - ContentService; imported lazily to avoid a cycle
    brand,  # noqa: ANN001 - ContentBrand
    source_url: str | None,
    company_id: uuid.UUID,
) -> list[Platform]:
    """Return the brand's linked platforms, linking ``source_url`` when needed."""
    linked = await service.list_brand_platforms(brand.id, company_id=company_id)
    if linked:
        return list(linked)
    if not source_url:
        raise ConflictError(
            "No monitored source is linked to this brand. Provide a public "
            "source URL in the start form or link a platform first."
        )
    try:
        validated = validate_public_url(source_url)
    except ValueError as exc:
        raise UnprocessableEntityError(
            f"The source URL is not allowed: {exc}"
        ) from exc
    normalised = normalise_url(validated)
    platform = await session.scalar(
        select(Platform).where(
            Platform.company_id == company_id,
            Platform.base_url == normalised,
        )
    )
    if platform is None:
        base_slug = slugify(brand.name) or "brand-source"
        platform = await PlatformService(session).create_platform(
            PlatformCreate(
                name=brand.display_name or brand.name,
                slug=f"{base_slug}-{uuid.uuid4().hex[:6]}",
                platform_type=PlatformType.WEBSITE,
                base_url=normalised,
                description=(
                    f"منبع پایش عمومی برند {brand.display_name or brand.name}"
                ),
                status=PlatformStatus.ACTIVE,
                integration_type=IntegrationType.MANUAL,
            ),
            company_id=company_id,
        )
    await service.set_brand_platforms(
        brand.id, company_id=company_id, platform_ids=[platform.id]
    )
    return [platform]
