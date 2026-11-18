"""Application authentication and account-approval endpoints.

Registration never grants access: new accounts are created in
``pending_approval`` state and every protected endpoint rejects them until an
administrator approves the account. Approval is a server-side decision stored
in PostgreSQL, not a client flag.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.deps import (
    get_auth_service,
    get_current_user,
    require_company_admin,
)
from app.core.exceptions import UnauthorizedError
from app.schemas.auth import (
    ApprovalDecision,
    AuthSession,
    LoginRequest,
    RegisterRequest,
    UserListResponse,
    UserRead,
)
from app.security.authorization import AuthenticatedUser, Principal
from app.security.tokens import create_access_token
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["authentication"])


def _session_for(user, request: Request) -> AuthSession:  # noqa: ANN001 - ORM user
    token, expires_at = create_access_token(user.id, request.app.state.settings)
    return AuthSession(
        user=UserRead.model_validate(user),
        token=token,
        token_expires_at=expires_at,
    )


def _admin_company_filter(principal: Principal) -> frozenset[uuid.UUID] | None:
    """Company ids an administrator may manage; ``None`` means every company."""
    if principal.is_wildcard():
        return None
    allowed: set[uuid.UUID] = set()
    for value in principal.company_ids:
        try:
            allowed.add(uuid.UUID(value))
        except ValueError:
            continue
    return frozenset(allowed)


@router.post(
    "/register",
    response_model=AuthSession,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthSession:
    """Create a pending account and return a token for the approval screen."""
    user = await service.register(payload)
    return _session_for(user, request)


@router.post("/login", response_model=AuthSession)
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthSession:
    """Authenticate an account.

    Pending accounts may sign in so they can see their approval status, but
    every tenant endpoint still rejects them until an administrator approves.
    """
    user = await service.authenticate(payload.email, payload.password)
    return _session_for(user, request)


@router.get("/me", response_model=UserRead)
async def me(
    identity: Annotated[AuthenticatedUser, Depends(get_current_user)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserRead:
    """Return the authenticated account, including its approval status."""
    user = await service.get_user(identity.id)
    if user is None:
        raise UnauthorizedError()
    return UserRead.model_validate(user)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    principal: Annotated[Principal, Depends(require_company_admin)],
    service: Annotated[AuthService, Depends(get_auth_service)],
    user_status: Literal["pending_approval", "approved", "rejected"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UserListResponse:
    """List accounts for administrator review (scoped to the admin's companies)."""
    users, total, pending = await service.list_users(
        status=user_status,
        limit=limit,
        offset=offset,
        company_ids=_admin_company_filter(principal),
    )
    return UserListResponse(
        items=[UserRead.model_validate(user) for user in users],
        total=total,
        pending=pending,
    )


@router.post("/users/{user_id}/approve", response_model=UserRead)
async def approve_user(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_company_admin)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> UserRead:
    """Approve a pending account so it can use the platform."""
    user = await service.decide(
        user_id,
        approved=True,
        actor_id=principal.subject,
        company_ids=_admin_company_filter(principal),
    )
    return UserRead.model_validate(user)


@router.post("/users/{user_id}/reject", response_model=UserRead)
async def reject_user(
    user_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require_company_admin)],
    service: Annotated[AuthService, Depends(get_auth_service)],
    payload: ApprovalDecision | None = None,
) -> UserRead:
    """Reject an account; rejected accounts cannot sign in."""
    del payload  # reserved for a future rejection-reason field
    user = await service.decide(
        user_id,
        approved=False,
        actor_id=principal.subject,
        company_ids=_admin_company_filter(principal),
    )
    return UserRead.model_validate(user)
