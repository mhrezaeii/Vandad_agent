"""Create the first company tenant for a fresh local deployment.

Usage:
    python -m app.cli.bootstrap_company --name "My Company" --slug my-company

The command is idempotent: an existing slug returns its current UUID.
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.database.session import create_engine, create_session_factory
from app.models import Company


async def bootstrap_company(name: str, slug: str) -> str:
    settings = get_settings()
    engine = create_engine(settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as session:
            company = await session.scalar(select(Company).where(Company.slug == slug))
            if company is None:
                company = Company(name=name, slug=slug, status="active")
                session.add(company)
                await session.commit()
                await session.refresh(company)
            return str(company.id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap a Vandad Agent company tenant.")
    parser.add_argument("--name", required=True, help="Display name for the company.")
    parser.add_argument("--slug", required=True, help="Unique lowercase company slug.")
    args = parser.parse_args()
    print(asyncio.run(bootstrap_company(args.name.strip(), args.slug.strip())))


if __name__ == "__main__":
    main()
