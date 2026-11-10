"""Data access layer. Repositories encapsulate queries; they never commit —
the unit of work (session dependency / service) owns transaction boundaries."""

from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.audit_repository import AuditEventRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.connection_repository import ConnectionRepository
from app.repositories.platform_repository import PlatformRepository

__all__ = [
    "ArtifactRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ConnectionRepository",
    "PlatformRepository",
]
