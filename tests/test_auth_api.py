"""Application authentication, approval enforcement and tenant isolation."""

from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from app.models import Company, User, UserRole, UserStatus
from app.security.passwords import hash_password, verify_password
from app.security.tokens import AuthTokenError, create_access_token, decode_access_token

VALID_PASSWORD = "Passw0rd-strong"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _register(client, email: str = "new.user@example.test") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "New User",
            "email": email,
            "password": VALID_PASSWORD,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------- hashing
def test_password_hash_round_trip_and_salting():
    first = hash_password(VALID_PASSWORD)
    second = hash_password(VALID_PASSWORD)
    assert first != second
    assert verify_password(VALID_PASSWORD, first) is True
    assert verify_password("wrong-password-1", first) is False
    assert verify_password(VALID_PASSWORD, "not-a-hash") is False


def test_token_signature_and_expiry():
    settings = get_settings()
    token, expires_at = create_access_token(uuid.uuid4(), settings, ttl_seconds=60)
    claims = decode_access_token(token, settings)
    assert claims.expires_at == expires_at
    with pytest.raises(AuthTokenError):
        decode_access_token(token + "x", settings)
    expired, _ = create_access_token(uuid.uuid4(), settings, ttl_seconds=-5)
    with pytest.raises(AuthTokenError):
        decode_access_token(expired, settings)


# ------------------------------------------------------------------ registration
async def test_registration_creates_pending_account(client):
    body = await _register(client)
    assert body["user"]["status"] == "pending_approval"
    assert body["user"]["role"] == "member"
    assert body["token"]
    assert body["user"]["email"] == "new.user@example.test"


async def test_duplicate_registration_is_rejected(client):
    await _register(client)
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "full_name": "Someone Else",
            "email": "NEW.USER@example.test",
            "password": VALID_PASSWORD,
        },
    )
    assert response.status_code == 409


async def test_weak_registration_payload_is_rejected(client):
    response = await client.post(
        "/api/v1/auth/register",
        json={"full_name": "A", "email": "bad-email", "password": "short"},
    )
    assert response.status_code == 422


# ------------------------------------------------------------- approval gating
async def test_pending_account_can_read_status_but_not_tenant_data(client):
    body = await _register(client)
    token = body["token"]

    me = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["status"] == "pending_approval"

    brands = await client.get("/api/v1/content/brands", headers=_auth(token))
    assert brands.status_code == 403
    assert brands.json()["error"]["code"] == "account_pending_approval"

    write_attempt = await client.post(
        "/api/v1/content/brands",
        headers=_auth(token),
        json={"name": "blocked-brand"},
    )
    assert write_attempt.status_code == 403


async def test_administrator_approval_unblocks_the_same_token_immediately(client):
    body = await _register(client)
    token = body["token"]
    user_id = body["user"]["id"]

    pending_list = await client.get("/api/v1/auth/users?status=pending_approval")
    assert pending_list.status_code == 200
    assert pending_list.json()["pending"] == 1
    assert pending_list.json()["items"][0]["id"] == user_id

    approved = await client.post(f"/api/v1/auth/users/{user_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_at"] is not None

    brands = await client.get("/api/v1/content/brands", headers=_auth(token))
    assert brands.status_code == 200
    # Tenant isolation still applies: every row belongs to the account's company.
    company_id = approved.json()["company_id"]
    assert all(item["company_id"] == company_id for item in brands.json())


async def test_rejected_account_cannot_sign_in(client):
    body = await _register(client)
    user_id = body["user"]["id"]
    rejected = await client.post(f"/api/v1/auth/users/{user_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "new.user@example.test", "password": VALID_PASSWORD},
    )
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "account_rejected"

    # A previously issued token must stop working as well.
    brands = await client.get(
        "/api/v1/content/brands", headers=_auth(body["token"])
    )
    assert brands.status_code == 403
    assert brands.json()["error"]["code"] == "account_rejected"


async def test_login_returns_session_and_normalizes_email(client):
    await _register(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "  NEW.USER@Example.Test ", "password": VALID_PASSWORD},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["email"] == "new.user@example.test"
    assert payload["token"]


async def test_login_with_wrong_password_is_generic(client):
    await _register(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "new.user@example.test", "password": "Wrong-pass-1"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Email or password is incorrect."


async def test_unknown_and_forged_tokens_are_rejected(client):
    missing = await client.get("/api/v1/auth/me")
    assert missing.status_code == 401

    forged = await client.get("/api/v1/auth/me", headers=_auth("not.a.token"))
    assert forged.status_code == 401

    settings = get_settings()
    token, _ = create_access_token(uuid.uuid4(), settings, ttl_seconds=60)
    unknown = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert unknown.status_code == 401


# ------------------------------------------------------------------ authorization
async def test_member_cannot_manage_accounts(client, db_session):
    body = await _register(client)
    await client.post(f"/api/v1/auth/users/{body['user']['id']}/approve")

    listing = await client.get("/api/v1/auth/users", headers=_auth(body["token"]))
    assert listing.status_code == 403


async def test_account_is_scoped_to_its_own_company(client, db_session):
    body = await _register(client)
    await client.post(f"/api/v1/auth/users/{body['user']['id']}/approve")

    from sqlalchemy import select

    default_company = (
        await db_session.execute(select(Company).where(Company.slug == "default"))
    ).scalar_one()
    other_company = Company(name="Other Co", slug="other-co-auth")
    db_session.add(other_company)
    await db_session.flush()

    own = await client.get("/api/v1/content/brands", headers=_auth(body["token"]))
    assert own.status_code == 200

    cross_tenant = await client.get(
        "/api/v1/content/brands",
        headers={**_auth(body["token"]), "X-Company-ID": str(other_company.id)},
    )
    assert cross_tenant.status_code == 403

    allowed = await client.get(
        "/api/v1/content/brands",
        headers={**_auth(body["token"]), "X-Company-ID": str(default_company.id)},
    )
    assert allowed.status_code == 200


async def test_administrator_only_sees_accounts_in_its_company(client, db_session):
    body = await _register(client)
    await client.post(f"/api/v1/auth/users/{body['user']['id']}/approve")

    other_company = Company(name="Other Co", slug="other-co-admin")
    db_session.add(other_company)
    await db_session.flush()
    outsider = User(
        email="outsider@example.test",
        full_name="Outsider",
        password_hash=hash_password(VALID_PASSWORD),
        status=UserStatus.PENDING_APPROVAL.value,
        role=UserRole.MEMBER.value,
        company_id=other_company.id,
    )
    db_session.add(outsider)
    await db_session.commit()

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "new.user@example.test", "password": VALID_PASSWORD},
    )
    member_token = login.json()["token"]

    # Promote the member to admin directly to exercise company-scoped listing.
    from sqlalchemy import select

    member = (
        await db_session.execute(
            select(User).where(User.email == "new.user@example.test")
        )
    ).scalar_one()
    member.role = UserRole.ADMIN.value
    await db_session.commit()

    listing = await client.get("/api/v1/auth/users", headers=_auth(member_token))
    assert listing.status_code == 200
    visible_emails = {item["email"] for item in listing.json()["items"]}
    assert "new.user@example.test" in visible_emails
    assert "outsider@example.test" not in visible_emails

    hidden = await client.post(
        f"/api/v1/auth/users/{outsider.id}/approve", headers=_auth(member_token)
    )
    assert hidden.status_code == 404
