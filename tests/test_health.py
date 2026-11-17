"""Liveness endpoint and correlation ID behavior."""

from __future__ import annotations

import uuid

from app.core.config import get_settings


async def test_health_returns_ok(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    settings = get_settings()
    assert body["status"] == "ok"
    assert body["service"] == settings.app_name
    assert body["version"] == settings.app_version
    assert body["environment"] == settings.environment
    assert body["timestamp"]


async def test_health_generates_correlation_id(client):
    response = await client.get("/api/v1/health")
    correlation_id = response.headers.get("x-request-id")
    assert correlation_id
    assert len(correlation_id) == 32  # uuid4().hex


async def test_health_echoes_provided_correlation_id(client):
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "corr-test-12345678"})
    assert response.headers["x-request-id"] == "corr-test-12345678"


async def test_health_rejects_unsafe_correlation_id(client):
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id with spaces!"})
    correlation_id = response.headers.get("x-request-id")
    assert correlation_id and correlation_id != "bad id with spaces!"


async def test_unknown_route_returns_error_envelope(client):
    response = await client.get(f"/api/v1/does-not-exist/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "http_404"
    assert body["error"]["correlation_id"]
