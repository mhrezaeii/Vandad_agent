from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Company


class CompanyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, company_id: object) -> Company | None:
        return await self.session.get(Company, company_id)

    async def get_by_slug(self, slug: str) -> Company | None:
        result = await self.session.execute(select(Company).where(Company.slug == slug))
        return result.scalar_one_or_none()

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(Company))
        return int(result.scalar_one())

    async def get_or_create_default(self) -> Company:
        """Return the singleton ``default`` company, creating it on first use.

        Platforms may be created without an explicit company (phase 1 has no
        company management endpoints); they then belong to this company.
        """
        company = await self.get_by_slug("default")
        if company is not None:
            return company
        company = Company(name="Default Company", slug="default")
        self.session.add(company)
        await self.session.flush()
        return company
