"""Test-only mock provider contract and registry behavior."""

from __future__ import annotations

import pytest

from app.integrations.video import (
    MockSceneVideoProvider,
    SceneJobState,
    SceneVideoProviderRegistry,
    SceneVideoProviderRegistryError,
    SceneVideoRequest,
)


def request(key: str = "stable-key") -> SceneVideoRequest:
    return SceneVideoRequest(
        scene_id="project:scene-1",
        prompt="A vertical product film.",
        duration_seconds=5,
        idempotency_key=key,
    )


async def test_mock_provider_completes_and_retrieves_a_fixture_video():
    provider = MockSceneVideoProvider(poll_steps=1)
    job_id = await provider.generate_video(request())
    status = await provider.get_generation_status(job_id)
    assert status.state == SceneJobState.COMPLETED.value
    payload = b"".join([chunk async for chunk in provider.retrieve_video(job_id)])
    assert payload[4:8] == b"ftyp"


async def test_mock_provider_is_idempotent_by_key():
    provider = MockSceneVideoProvider()
    first = await provider.generate_video(request())
    second = await provider.generate_video(request())
    assert first == second
    assert len(provider.jobs) == 1


async def test_mock_provider_enforces_its_test_quota():
    provider = MockSceneVideoProvider(quota_limit_per_day=1)
    await provider.generate_video(request("one"))
    with pytest.raises(Exception):
        await provider.generate_video(request("two"))


def test_registry_rejects_duplicate_provider_ids():
    registry = SceneVideoProviderRegistry()
    registry.register(MockSceneVideoProvider())
    with pytest.raises(SceneVideoProviderRegistryError):
        registry.register(MockSceneVideoProvider())
