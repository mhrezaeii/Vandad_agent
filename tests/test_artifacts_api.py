"""Artifact API tests: upload/download streaming, tenant isolation, no path leaks."""

from __future__ import annotations

import hashlib
import io
import uuid

import pytest


@pytest.fixture
def headers(company_and_platform) -> dict:
    _, platform = company_and_platform
    return {"X-Company-ID": str(platform.company_id)}


async def _execution(client, headers) -> tuple[str, dict]:
    """Create a published workflow execution; returns (execution_id, acting_headers).

    The platforms API attaches new platforms to the default company, so the
    acting company for everything downstream is the one in the response.
    """
    platform_resp = await client.post(
        "/api/v1/platforms",
        json={"name": f"art-{uuid.uuid4().hex[:8]}", "platform_type": "api_service"},
        headers=headers,
    )
    acting = {"X-Company-ID": platform_resp.json()["company_id"]}
    workflow = await client.post(
        "/api/v1/workflows",
        json={
            "name": f"art-wf-{uuid.uuid4().hex[:8]}",
            "nodes": [{"node_key": "start", "node_type": "trigger.manual", "name": "t"}],
            "edges": [],
        },
        headers=acting,
    )
    wid = workflow.json()["id"]
    await client.post(f"/api/v1/workflows/{wid}/publish", headers=acting)
    executed = await client.post(
        f"/api/v1/workflows/{wid}/execute", json={}, headers=acting
    )
    assert executed.status_code == 202, executed.text
    return executed.json()["id"], acting


async def test_upload_download_roundtrip(client, headers):
    execution_id, headers = await _execution(client, headers)
    payload = b"VIDEO-BYTES" * 10000
    expected_sha = hashlib.sha256(payload).hexdigest()

    uploaded = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "video"},
        files={"file": ("render-final.mp4", io.BytesIO(payload), "video/mp4")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["kind"] == "video"
    assert body["size_bytes"] == len(payload)
    assert body["sha256"] == expected_sha
    assert body["content_type"] == "video/mp4"
    assert body["original_filename"] == "render-final.mp4"
    # No storage key / path / filesystem detail may leak.
    assert "storage_key" not in body
    assert "/" not in str(body.get("sha256"))
    assert "executions/" not in str(body)

    listed = await client.get(
        f"/api/v1/executions/{execution_id}/artifacts", headers=headers
    )
    assert listed.json()["total"] == 1

    downloaded = await client.get(
        f"/api/v1/artifacts/{body['id']}/content", headers=headers
    )
    assert downloaded.status_code == 200
    assert downloaded.content == payload
    assert downloaded.headers["content-type"].startswith("video/mp4")
    assert int(downloaded.headers["content-length"]) == len(payload)


async def test_upload_validates_kind(client, headers):
    execution_id, headers = await _execution(client, headers)
    response = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "nuclear-codes"},
        files={"file": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 422


async def test_upload_requires_owned_execution(client, db_session, headers, company_and_platform):
    """Uploading to another company's execution reads as missing (404)."""
    from app.models import Company

    _, platform = company_and_platform
    execution_id, headers = await _execution(client, headers)

    stranger = Company(name="Art Rival", slug="art-rival")
    db_session.add(stranger)
    await db_session.commit()
    foreign = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "video"},
        files={"file": ("x.mp4", io.BytesIO(b"x"), "video/mp4")},
        headers={"X-Company-ID": str(stranger.id)},
    )
    assert foreign.status_code == 404


async def test_artifact_tenant_isolation_on_read_and_delete(client, db_session, headers, company_and_platform):
    from app.models import Company

    execution_id, headers = await _execution(client, headers)
    uploaded = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "script"},
        files={"file": ("script.txt", io.BytesIO(b"persian script"), "text/plain")},
        headers=headers,
    )
    artifact_id = uploaded.json()["id"]

    stranger = Company(name="Art Sneak", slug="art-sneak")
    db_session.add(stranger)
    await db_session.commit()
    foreign_headers = {"X-Company-ID": str(stranger.id)}

    assert (
        await client.get(f"/api/v1/artifacts/{artifact_id}", headers=foreign_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=foreign_headers)
    ).status_code == 404
    assert (
        await client.delete(f"/api/v1/artifacts/{artifact_id}", headers=foreign_headers)
    ).status_code == 404

    # The owner retains full access.
    assert (
        await client.get(f"/api/v1/artifacts/{artifact_id}", headers=headers)
    ).status_code == 200


async def test_download_missing_artifact_404(client, headers):
    _, headers = await _execution(client, headers)
    response = await client.get(
        f"/api/v1/artifacts/{uuid.uuid4()}/content", headers=headers
    )
    assert response.status_code == 404


async def test_delete_artifact_removes_content(client, headers):
    execution_id, headers = await _execution(client, headers)
    uploaded = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "image"},
        files={"file": ("img.png", io.BytesIO(b"png-bytes"), "image/png")},
        headers=headers,
    )
    artifact_id = uploaded.json()["id"]
    deleted = await client.delete(f"/api/v1/artifacts/{artifact_id}", headers=headers)
    assert deleted.status_code == 204
    assert (
        await client.get(f"/api/v1/artifacts/{artifact_id}/content", headers=headers)
    ).status_code == 404


async def test_artifact_metadata_has_no_filesystem_paths(client, headers, app):
    execution_id, headers = await _execution(client, headers)
    uploaded = await client.post(
        f"/api/v1/executions/{execution_id}/artifacts",
        params={"kind": "subtitle"},
        files={"file": ("subs.srt", io.BytesIO("1\n00:00:00,000 --> 00:00:01,000\nسلام".encode()), "text/plain")},
        headers=headers,
    )
    body_text = uploaded.text
    assert "data/artifacts" not in body_text
    assert "\\" not in body_text
    storage_root = app.state.artifact_storage._root  # noqa: SLF001 - test assertion
    assert str(storage_root) not in body_text
