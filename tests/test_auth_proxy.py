"""Production identity-proxy authentication and role enforcement."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.deps import company_scope, get_principal
from app.core.config import Settings
from app.core.exceptions import ForbiddenError, UnauthorizedError, UnprocessableEntityError


def _request(*, method="GET", headers=None, companies=None, roles="viewer", secret="proxy-secret-0123456789-abcdef-0123456789"):
    company_ids = companies or [str(uuid.uuid4())]
    values = {
        "X-Auth-Proxy-Secret": secret,
        "X-Auth-Request-User": "operator@example.test",
        "X-Auth-Request-Company-IDs": ",".join(company_ids),
        "X-Auth-Request-Roles": roles,
        **(headers or {}),
    }
    settings = Settings(
        environment="production",
        bootstrap_principal_company_ids=",".join(company_ids),
        auth_proxy_shared_secret="proxy-secret-0123456789-abcdef-0123456789",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            principal=None,
        )
    )
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in values.items()]
    return Request({"type": "http", "method": method, "headers": raw_headers, "app": app})


def test_production_identity_requires_proxy_shared_secret():
    request = _request(headers={"X-Auth-Proxy-Secret": ""})
    with pytest.raises(UnauthorizedError):
        get_principal(request)


def test_production_identity_maps_user_company_and_roles():
    company_id = str(uuid.uuid4())
    request = _request(companies=[company_id], roles="viewer,operator")
    principal = get_principal(request)

    assert principal.subject == "operator@example.test"
    assert principal.company_ids == frozenset({company_id})
    assert principal.roles == frozenset({"viewer", "operator"})


async def test_viewer_can_read_but_cannot_mutate_company_scope():
    company_id = str(uuid.uuid4())
    read_scope = await company_scope(_request(method="GET", companies=[company_id]))
    assert str(read_scope.company_id) == company_id

    with pytest.raises(ForbiddenError):
        await company_scope(_request(method="POST", companies=[company_id]))


async def test_multi_company_identity_must_select_company_header():
    request = _request(companies=[str(uuid.uuid4()), str(uuid.uuid4())])
    with pytest.raises(UnprocessableEntityError):
        await company_scope(request)
