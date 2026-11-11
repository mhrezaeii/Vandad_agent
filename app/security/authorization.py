"""Company-level authorization boundary.

Phase 2 has no user authentication yet. Instead of silently treating every
caller as an administrator, this module defines the explicit seam:

- ``Principal``: the acting identity, carrying the set of company IDs it may
  operate for.
- ``authorize_company_access``: the single decision point used by services and
  dependencies; raises ``ForbiddenError`` on denial.

Principals are built from settings (``PLATFORM_BOOTSTRAP_COMPANY_IDS``).
The wildcard ``"*"`` is honored only outside production — ``create_app``
refuses to start in production with a wildcard principal configured, so the
default-deny posture cannot be accidentally relaxed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.exceptions import ForbiddenError


@dataclass(frozen=True)
class Principal:
    """Acting identity. ``company_ids`` may contain the wildcard '*'."""

    subject: str
    company_ids: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)

    def is_wildcard(self) -> bool:
        return "*" in self.company_ids

    def may_access_company(self, company_id: uuid.UUID) -> bool:
        return self.is_wildcard() or str(company_id) in self.company_ids


@dataclass(frozen=True)
class CompanyScope:
    """Resolved authorization result for one company."""

    company_id: uuid.UUID
    principal: Principal


class AuthorizationError(ForbiddenError):
    pass


def authorize_company_access(principal: Principal, company_id: uuid.UUID) -> CompanyScope:
    """Authorize acting on *company_id*; raise ForbiddenError when denied."""
    if not principal.may_access_company(company_id):
        raise ForbiddenError(
            "The acting principal is not authorized for this company.",
            details=[{"field": "company_id", "value": str(company_id)}],
        )
    return CompanyScope(company_id=company_id, principal=principal)


def build_principal_from_settings(settings: Settings) -> Principal:
    """Build the bootstrap principal. Production never accepts the wildcard."""
    raw = settings.bootstrap_principal_company_ids.strip()
    if raw == "*":
        if settings.is_production:
            raise ForbiddenError(
                "bootstrap_principal_company_ids='*' is not allowed in production; "
                "configure explicit company IDs."
            )
        return Principal(
            subject="bootstrap",
            company_ids=frozenset({"*"}),
            roles=frozenset({"owner", "admin", "operator", "viewer"}),
        )
    ids = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return Principal(
        subject="bootstrap",
        company_ids=ids,
        roles=frozenset({"owner", "admin", "operator", "viewer"}),
    )
