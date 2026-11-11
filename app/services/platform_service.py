from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.slug import slugify
from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.models import Platform
from app.repositories import CompanyRepository, PlatformRepository
from app.schemas.platform import PlatformCreate, PlatformUpdate
from app.services.audit_service import AuditService

_PLATFORM_RESOURCE = "platform"


def _is_unique_violation(exc: IntegrityError) -> bool:
    return "unique" in str(exc.orig).lower() if exc.orig is not None else False


class PlatformService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.platforms = PlatformRepository(session)
        self.companies = CompanyRepository(session)
        self.audit = AuditService(session)

    async def create_platform(self, data: PlatformCreate, *, company_id: uuid.UUID) -> Platform:
        if data.company_id is not None and data.company_id != company_id:
            raise ForbiddenError("A platform cannot be created for another company.")
        company = await self._resolve_company(company_id)
        slug = data.slug or slugify(data.name)

        if await self.platforms.get_by_slug(company.id, slug) is not None:
            raise ConflictError(
                f"A platform with slug '{slug}' already exists for this company.",
                details=[{"field": "slug", "value": slug}],
            )

        platform = Platform(
            company_id=company.id,
            name=data.name,
            slug=slug,
            platform_type=data.platform_type.value,
            base_url=str(data.base_url) if data.base_url else None,
            description=data.description,
            status=data.status.value,
            integration_type=data.integration_type.value,
            configuration=data.configuration,
        )
        try:
            platform = await self.platforms.add(platform)
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise ConflictError(
                    f"A platform with slug '{slug}' already exists for this company.",
                    details=[{"field": "slug", "value": slug}],
                ) from exc
            raise

        self.audit.record(
            event_type="platform.created",
            action="create",
            company_id=company.id,
            platform_id=platform.id,
            resource_type=_PLATFORM_RESOURCE,
            resource_id=platform.id,
            metadata={"name": platform.name, "slug": platform.slug, "platform_type": platform.platform_type},
        )
        return platform

    async def get_platform(self, platform_id: uuid.UUID, *, company_id: uuid.UUID) -> Platform:
        platform = await self.platforms.get(platform_id)
        if platform is None or platform.company_id != company_id:
            raise self._not_found(platform_id)
        return platform

    async def list_platforms(
        self,
        *,
        company_id: uuid.UUID,
        status: str | None = None,
        platform_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Platform], int]:
        return await self.platforms.list(
            company_id=company_id, status=status, platform_type=platform_type, limit=limit, offset=offset
        )

    async def update_platform(
        self, platform_id: uuid.UUID, data: PlatformUpdate, *, company_id: uuid.UUID
    ) -> Platform:
        platform = await self.get_platform(platform_id, company_id=company_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=False)

        if "company_id" in changes and changes["company_id"] != company_id:
            raise ForbiddenError("A platform cannot be moved to another company.")
        changes.pop("company_id", None)

        if "slug" in changes and changes["slug"] != platform.slug:
            target_company_id = changes.get("company_id", platform.company_id)
            if await self.platforms.get_by_slug(target_company_id, changes["slug"]) is not None:
                raise ConflictError(
                    f"A platform with slug '{changes['slug']}' already exists for this company.",
                    details=[{"field": "slug", "value": changes["slug"]}],
                )

        if "base_url" in changes and changes["base_url"] is not None:
            changes["base_url"] = str(changes["base_url"])

        for field, value in changes.items():
            setattr(platform, field, value)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            if _is_unique_violation(exc):
                raise ConflictError(
                    "Another platform already uses this slug for the target company.",
                    details=[{"field": "slug", "value": platform.slug}],
                ) from exc
            raise

        # Reload server-managed columns (updated_at) before serialization.
        await self.session.refresh(platform)

        self.audit.record(
            event_type="platform.updated",
            action="update",
            company_id=platform.company_id,
            platform_id=platform.id,
            resource_type=_PLATFORM_RESOURCE,
            resource_id=platform.id,
            metadata={"changed_fields": sorted(changes.keys())},
        )
        return platform

    async def delete_platform(self, platform_id: uuid.UUID, *, company_id: uuid.UUID) -> None:
        platform = await self.get_platform(platform_id, company_id=company_id)
        try:
            await self.platforms.delete(platform)
        except IntegrityError as exc:
            raise ConflictError(
                "Platform cannot be deleted because dependent resources exist."
            ) from exc

        self.audit.record(
            event_type="platform.deleted",
            action="delete",
            company_id=platform.company_id,
            resource_type=_PLATFORM_RESOURCE,
            resource_id=platform.id,
            metadata={"name": platform.name, "slug": platform.slug},
        )

    async def _resolve_company(self, company_id: uuid.UUID):
        company = await self.companies.get(company_id)
        if company is None:
            raise NotFoundError(
                "Company not found.", details=[{"field": "company_id", "value": str(company_id)}]
            )
        return company

    @staticmethod
    def _not_found(platform_id: uuid.UUID) -> NotFoundError:
        return NotFoundError(
            "Platform not found.", details=[{"field": "platform_id", "value": str(platform_id)}]
        )
