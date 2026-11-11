"""Deterministic, local scene-video provider used for development and tests.

It performs **no** AI generation and **no** network calls: it returns a small
synthetic MP4-shaped payload after a configurable number of status polls. That
lets the full pipeline (submission -> polling -> download -> assembly) run
without spending credits or requiring ffmpeg on the host.

The payload is a container-shaped fixture only; it is not a playable AI video.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.integrations.video.base import (
    InMemorySceneVideoProvider,
    SceneGenerationStatus,
    SceneJobState,
    SceneVideoCompositionRequest,
    SceneVideoPermanentError,
    SceneVideoRequest,
)


def _synthetic_mp4(size_bytes: int = 4096) -> bytes:
    """Build a deterministic, container-shaped payload (not real video)."""
    header = (
        b"\x00\x00\x00\x18ftypmp42"
        b"\x00\x00\x02\x00"
        b"mp42isom"
        b"\x00\x00\x00\x08free"
    )
    if size_bytes <= len(header):
        return header[:size_bytes]
    filler = bytes(index % 251 for index in range(size_bytes - len(header)))
    return header + filler


class MockSceneVideoProvider(InMemorySceneVideoProvider):
    """In-memory provider with deterministic polling behaviour."""

    provider_id = "mock"
    display_name = "Local mock scene-video provider"
    supports_cancellation = True
    supports_composition = True
    requires_network = False

    def __init__(
        self,
        *,
        poll_steps: int = 2,
        quota_limit_per_day: int | None = None,
        fail_scene_ids: set[str] | None = None,
        payload_size: int = 4096,
    ) -> None:
        super().__init__(quota_limit_per_day=quota_limit_per_day)
        self.poll_steps = max(1, int(poll_steps))
        self.payload_size = max(32, int(payload_size))
        self.fail_scene_ids = set(fail_scene_ids or set())
        self._jobs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, str] = {}

    # ------------------------------------------------------------- submission
    async def generate_video(self, request: SceneVideoRequest) -> str:
        if request.idempotency_key and request.idempotency_key in self._idempotency:
            return self._idempotency[request.idempotency_key]
        if request.scene_id in self.fail_scene_ids:
            raise SceneVideoPermanentError(
                "The mock provider was configured to fail this scene.",
                code="mock_scene_failure",
            )
        self._consume_quota()
        job_id = f"mock-{uuid.uuid4().hex}"
        self._jobs[job_id] = {
            "request": request,
            "state": SceneJobState.QUEUED.value,
            "polls": 0,
            "payload": _synthetic_mp4(self.payload_size),
        }
        if request.idempotency_key:
            self._idempotency[request.idempotency_key] = job_id
        return job_id

    async def compose_video(self, request: SceneVideoCompositionRequest) -> str:
        """Simulate provider-side editing/composition for integration tests."""
        if request.idempotency_key and request.idempotency_key in self._idempotency:
            return self._idempotency[request.idempotency_key]
        self._consume_quota()
        job_id = f"mock-compose-{uuid.uuid4().hex}"
        synthetic_request = SceneVideoRequest(
            scene_id=f"composition:{request.project_id}",
            prompt="AI-native final video composition",
            duration_seconds=30,
            width=request.width,
            height=request.height,
            idempotency_key=request.idempotency_key,
        )
        self._jobs[job_id] = {
            "request": synthetic_request,
            "state": SceneJobState.QUEUED.value,
            "polls": 0,
            "payload": _synthetic_mp4(self.payload_size),
        }
        if request.idempotency_key:
            self._idempotency[request.idempotency_key] = job_id
        return job_id

    # ----------------------------------------------------------------- status
    async def get_generation_status(self, job_id: str) -> SceneGenerationStatus:
        job = self._jobs.get(job_id)
        if job is None:
            raise SceneVideoPermanentError(
                "Unknown mock scene job.", code="mock_job_not_found"
            )
        state = job["state"]
        if state in (
            SceneJobState.COMPLETED.value,
            SceneJobState.FAILED.value,
            SceneJobState.CANCELLED.value,
        ):
            return self._status_from_job(job_id, job)

        job["polls"] += 1
        if job["polls"] >= self.poll_steps:
            job["state"] = SceneJobState.COMPLETED.value
        else:
            job["state"] = SceneJobState.GENERATING.value
        return self._status_from_job(job_id, job)

    # --------------------------------------------------------------- retrieval
    async def retrieve_video(self, job_id: str) -> AsyncIterator[bytes]:
        job = self._jobs.get(job_id)
        if job is None:
            raise SceneVideoPermanentError(
                "Unknown mock scene job.", code="mock_job_not_found"
            )
        if job["state"] != SceneJobState.COMPLETED.value:
            raise SceneVideoPermanentError(
                "The mock scene job is not completed.", code="mock_job_not_completed"
            )
        payload: bytes = job["payload"]
        chunk_size = 1024
        for offset in range(0, len(payload), chunk_size):
            yield payload[offset : offset + chunk_size]

    async def cancel_generation(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None:
            return False
        if job["state"] in (
            SceneJobState.COMPLETED.value,
            SceneJobState.FAILED.value,
            SceneJobState.CANCELLED.value,
        ):
            return job["state"] == SceneJobState.CANCELLED.value
        job["state"] = SceneJobState.CANCELLED.value
        return True

    # ---------------------------------------------------------------- helpers
    def _status_from_job(self, job_id: str, job: dict[str, Any]) -> SceneGenerationStatus:
        request: SceneVideoRequest = job["request"]
        state = job["state"]
        progress = None
        if state == SceneJobState.GENERATING.value:
            progress = round(job["polls"] / self.poll_steps, 3)
        elif state == SceneJobState.COMPLETED.value:
            progress = 1.0
        return SceneGenerationStatus(
            job_id=job_id,
            state=state,
            progress=progress,
            video_url=f"mock://{job_id}" if state == SceneJobState.COMPLETED.value else None,
            duration_seconds=request.duration_seconds if state == SceneJobState.COMPLETED.value else None,
            raw={"provider": self.provider_id, "polls": job["polls"]},
        )

    @property
    def jobs(self) -> dict[str, dict[str, Any]]:
        """Expose job bookkeeping for assertions in tests."""
        return self._jobs
