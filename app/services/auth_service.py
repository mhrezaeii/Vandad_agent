"""Application account lifecycle: registration, login and approval decisions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import ConflictError, NotFoundError, UnauthorizedError
from app.models import Company, User, UserRole, UserStatus
from app.models.audit_event import ActorType
from app.schemas.auth import RegisterRequest
from app.security.passwords import hash_password, verify_password
from app.services.audit_service import AuditService

USER_RESOURCE = "user"
_INVALID_CREDENTIALS = "Email or password is incorrect."
# A syntactically valid scrypt digest of an unguessable value. Verifying against
# it keeps failed logins for unknown accounts as slow as those for known ones.
_DUMMY_HASH = (
    "scrypt$16384$8$1$MDAwMDAwMDAwMDAwMDAwMA==$"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
)


class AuthService:
    """Account operations bound to one unit of work."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.audit = AuditService(session)

    # ------------------------------------------------------------------ lookup
    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def list_users(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        company_ids: frozenset[uuid.UUID] | None = None,
    ) -> tuple[list[User], int, int]:
        statement = select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
        count_statement = select(func.count()).select_from(User)
        if status:
            statement = statement.where(User.status == status)
            count_statement = count_statement.where(User.status == status)
        if company_ids is not None:
            statement = statement.where(User.company_id.in_(company_ids))
            count_statement = count_statement.where(User.company_id.in_(company_ids))
        users = list((await self.session.execute(statement)).scalars().all())
        total = int((await self.session.execute(count_statement)).scalar_one())
        pending_statement = (
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.PENDING_APPROVAL.value)
        )
        if company_ids is not None:
            pending_statement = pending_statement.where(
                User.company_id.in_(company_ids)
            )
        pending = int((await self.session.execute(pending_statement)).scalar_one())
        return users, total, pending

    # ------------------------------------------------------------- registration
    async def register(self, payload: RegisterRequest) -> User:
        if await self.get_by_email(payload.email) is not None:
            raise ConflictError(
                "An account with this email already exists.",
                details=[{"field": "email"}],
            )

        role = UserRole.MEMBER.value
        status = UserStatus.PENDING_APPROVAL.value
        if self.settings.auth_bootstrap_first_admin and await self._has_no_admin():
            role = UserRole.ADMIN.value
            status = UserStatus.APPROVED.value

        company_id = await self._resolve_default_company_id()
        user = User(
            email=payload.email,
            full_name=payload.full_name,
            password_hash=hash_password(payload.password),
            status=status,
            role=role,
            company_id=company_id,
        )
        if status == UserStatus.APPROVED.value:
            user.approved_at = datetime.now(UTC)
            user.approved_by = "bootstrap"
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # concurrent registration for the same email
            await self.session.rollback()
            raise ConflictError(
                "An account with this email already exists.",
                details=[{"field": "email"}],
            ) from exc

        self.audit.record(
            event_type="auth.user_registered",
            action="register",
            actor_type=ActorType.USER,
            actor_id=str(user.id),
            company_id=company_id,
            resource_type=USER_RESOURCE,
            resource_id=user.id,
            metadata={"status": status, "role": role},
        )
        await self.session.flush()
        # Server defaults are not returned by INSERT; reload so response
        # serialization never triggers lazy IO outside the session.
        await self.session.refresh(user)
        return user

    async def _has_no_admin(self) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.role == UserRole.ADMIN.value,
                User.status == UserStatus.APPROVED.value,
            )
        )
        return int(result.scalar_one()) == 0

    async def _resolve_default_company_id(self) -> uuid.UUID:
        result = await self.session.execute(select(Company).where(Company.slug == "default"))
        company = result.scalar_one_or_none()
        if company is not None:
            return company.id
        company = Company(name="Default Company", slug="default")
        self.session.add(company)
        try:
            await self.session.flush()
        except IntegrityError:
            await self.session.rollback()
            result = await self.session.execute(
                select(Company).where(Company.slug == "default")
            )
            return result.scalar_one().id
        return company.id

    # --------------------------------------------------------------------- login
    async def authenticate(self, email: str, password: str) -> User:
        user = await self.get_by_email(email)
        stored = user.password_hash if user is not None else _DUMMY_HASH
        password_ok = verify_password(password, stored)
        if user is None or not password_ok:
            raise UnauthorizedError(_INVALID_CREDENTIALS)
        if user.status == UserStatus.REJECTED.value:
            raise UnauthorizedError(
                "This account was rejected. Contact an administrator.",
                code="account_rejected",
            )
        user.last_login_at = datetime.now(UTC)
        await self.session.flush()
        return user

    # ------------------------------------------------------------------ approval
    async def decide(
        self,
        user_id: uuid.UUID,
        *,
        approved: bool,
        actor_id: str,
        company_ids: frozenset[uuid.UUID] | None = None,
    ) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("User not found.", details=[{"user_id": str(user_id)}])
        if company_ids is not None and (
            user.company_id is None or user.company_id not in company_ids
        ):
            # Do not disclose accounts outside the administrator's companies.
            raise NotFoundError("User not found.", details=[{"user_id": str(user_id)}])
        target_status = (
            UserStatus.APPROVED.value if approved else UserStatus.REJECTED.value
        )
        if user.status == target_status:
            return user

        if approved:
            user.approved_at = datetime.now(UTC)
        else:
            user.approved_at = None
        user.status = target_status
        user.approved_by = actor_id

        self.audit.record(
            event_type="auth.user_approval_changed",
            action="approve" if approved else "reject",
            actor_type=ActorType.USER,
            actor_id=actor_id,
            company_id=user.company_id,
            resource_type=USER_RESOURCE,
            resource_id=user.id,
            metadata={"status": user.status},
        )
        await self.session.flush()
        await self.session.refresh(user)
        return user
