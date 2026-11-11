"""Video agent approval and tenant boundary checks."""

from __future__ import annotations

import uuid

import pytest

from app.agents.runtime import AgentExecutionContext, VideoProductionAgentHandler
from app.core.exceptions import ConflictError, NotFoundError
from app.models import Company, ContentBrand, ContentProject, WorkflowExecution


def _context(company_id, project_id, session_factory):
    return AgentExecutionContext(
        company_id=company_id,
        task_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        worker_id="video-agent-test",
        payload={"project_id": str(project_id)},
        session_factory=session_factory,
        provider_registry=object(),
        provider_executor=object(),
        master_key_box=object(),
        workflow_queue=object(),
        scene_video_registry=object(),
    )


async def _project_for_company(db_session, company_id, *, status="draft", video_mode="scene"):
    brand = ContentBrand(
        company_id=company_id,
        name=f"brand-{uuid.uuid4().hex[:8]}",
        display_name="Test Brand",
        language="fa-IR",
        tone="neutral",
        status="active",
    )
    db_session.add(brand)
    await db_session.flush()
    project = ContentProject(
        company_id=company_id,
        brand_id=brand.id,
        topic="Test video",
        status=status,
        video_mode=video_mode,
    )
    db_session.add(project)
    await db_session.commit()
    return project


async def test_video_agent_cannot_read_foreign_company_project(db_session, session_factory):
    owner = Company(name="Video Owner", slug=f"video-owner-{uuid.uuid4().hex[:8]}")
    stranger = Company(name="Video Stranger", slug=f"video-stranger-{uuid.uuid4().hex[:8]}")
    db_session.add_all([owner, stranger])
    await db_session.flush()
    project = await _project_for_company(db_session, owner.id)

    with pytest.raises(NotFoundError):
        await VideoProductionAgentHandler().execute(
            "video.scene.generate",
            _context(stranger.id, project.id, session_factory),
        )


async def test_video_agent_requires_project_approval(db_session, session_factory):
    company = Company(name="Video Approval", slug=f"video-approval-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    project = await _project_for_company(db_session, company.id, status="draft")

    with pytest.raises(ConflictError, match="Human approval"):
        await VideoProductionAgentHandler().execute(
            "video.scene.generate",
            _context(company.id, project.id, session_factory),
        )


async def test_video_agent_replays_existing_workflow_after_worker_restart(
    db_session, session_factory
):
    company = Company(name="Video Replay", slug=f"video-replay-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    execution = WorkflowExecution(
        company_id=company.id,
        workflow_name="scene-video-test",
        workflow_type="video_production",
        status="running",
    )
    db_session.add(execution)
    await db_session.flush()
    project = await _project_for_company(
        db_session,
        company.id,
        status="assembling",
        video_mode="scene",
    )
    project.video_workflow_execution_id = execution.id
    await db_session.commit()

    result = await VideoProductionAgentHandler().execute(
        "video.scene.generate",
        _context(company.id, project.id, session_factory),
    )
    assert result["replayed"] is True
    assert result["workflow_execution_id"] == str(execution.id)


async def test_video_agent_rejects_mismatched_pipeline_mode(db_session, session_factory):
    company = Company(name="Video Mode", slug=f"video-mode-{uuid.uuid4().hex[:8]}")
    db_session.add(company)
    await db_session.flush()
    project = await _project_for_company(
        db_session, company.id, status="approved", video_mode="avatar"
    )

    with pytest.raises(ConflictError, match="does not match"):
        await VideoProductionAgentHandler().execute(
            "video.scene.generate",
            _context(company.id, project.id, session_factory),
        )
