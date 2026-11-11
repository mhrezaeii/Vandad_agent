"""Company-level authorization boundary.

Production requests use a trusted OIDC/auth proxy identity. The API verifies
a shared proxy secret before accepting user, company-membership and role
headers. The proxy must strip client-supplied identity headers before injecting
its own. Local development retains the explicit bootstrap principal.

- ``Principal``: the acting identity, company memberships and roles.
- ``authorize_company_access``: the single company-scope decision point.
- ``require_company_admin``: protects owner/admin-only control-plane actions.

The wildcard bootstrap principal is honored only outside production.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.exceptions import ForbiddenError


@dataclass(frozen=True)
class Principal:
    """Acting identity, memberships and roles. Wildcard is development-only."""

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
