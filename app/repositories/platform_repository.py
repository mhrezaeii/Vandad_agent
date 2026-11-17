from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Platform


class PlatformRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, platform: Platform) -> Platform:
        self.session.add(platform)
        await self.session.flush()
        return platform

    async def get(self, platform_id: uuid.UUID) -> Platform | None:
        return await self.session.get(Platform, platform_id)

    async def get_by_slug(self, company_id: uuid.UUID, slug: str) -> Platform | None:
        result = await self.session.execute(
            select(Platform).where(Platform.company_id == company_id, Platform.slug == slug)
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        company_id: uuid.UUID | None = None,
        status: str | None = None,
        platform_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Platform], int]:
        """Return (items, total) with filtering and pagination applied."""
        conditions = []
        if company_id is not None:
            conditions.append(Platform.company_id == company_id)
        if status is not None:
            conditions.append(Platform.status == status)
        if platform_type is not None:
            conditions.append(Platform.platform_type == platform_type)

        total_query = select(func.count()).select_from(Platform).where(*conditions)
        total = int((await self.session.execute(total_query)).scalar_one())

        items_query = (
            select(Platform)
            .where(*conditions)
            .order_by(Platform.created_at.desc(), Platform.id)
            .limit(limit)
            .offset(offset)
        )
        items = list((await self.session.execute(items_query)).scalars().all())
        return items, total

    async def delete(self, platform: Platform) -> None:
        await self.session.delete(platform)
        await self.session.flush()
