"""Strict payload validation for code-owned agent capabilities."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.agents.contracts import validate_capability_payload


def test_monitoring_discovery_payload_is_normalized_and_bounded():
    platform_id = uuid.uuid4()
    payload = validate_capability_payload(
        "monitoring",
        "platform.discover",
        {"platform_id": str(platform_id), "max_pages": 25},
    )
    assert payload == {"platform_id": str(platform_id), "max_pages": 99}


def test_monitoring_discovery_rejects_out_of_range_limits():
    with pytest.raises(ValueError, match="capability contract"):
        validate_capability_payload(
            "monitoring",
            "platform.discover",
            {"platform_id": str(uuid.uuid4()), "max_pages": 99},
        )


def test_monitoring_discovery_rejects_unknown_fields():
    with pytest.raises(ValueError, match="capability contract"):
        validate_capability_payload(
            "monitoring",
            "platform.discover",
            {"platform_id": str(uuid.uuid4()), "connection_id": str(uuid.uuid4())},
        )


def test_content_scenario_requires_project_and_company_connection():
    with pytest.raises(ValueError, match="capability contract"):
        validate_capability_payload(
            "content", "content.scenario.generate", {"topic": "launch"}
        )


def test_video_scene_contract_rejects_unknown_provider_fields():
    with pytest.raises(ValueError, match="capability contract"):
        validate_capability_payload(
            "video",
            "video.scene.generate",
            {"project_id": str(uuid.uuid4()), "api_key": "must-not-pass"},
        )


def test_video_avatar_requires_connection_id():
    with pytest.raises(ValueError, match="capability contract"):
        validate_capability_payload(
            "video", "video.avatar.generate", {"project_id": str(uuid.uuid4())}
        )
