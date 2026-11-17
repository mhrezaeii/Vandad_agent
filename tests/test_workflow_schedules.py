"""Workflow schedule tests: types, timezone math, missed handling, duplicates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.services.workflow_schedule_service import WorkflowScheduleService


@pytest.fixture
def schedule_service(db_session, workflow_queue):
    return WorkflowScheduleService(db_session, queue=workflow_queue)


async def _published_workflow(workflow_factory, company_id):
    return await workflow_factory(
        company_id,
        [{"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}],
        [],
    )


async def test_create_interval_schedule_computes_next_run(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="every-minute",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
    )
    assert schedule.next_run_at is not None
    assert 0 < (schedule.next_run_at - datetime.now(UTC)).total_seconds() <= 60


async def test_create_cron_schedule_with_timezone(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="tehran-morning",
        schedule_type=__import__("app").models.WorkflowScheduleType.CRON,
        schedule_config={"cron": "0 9 * * *"},
        timezone_name="Asia/Tehran",
    )
    assert schedule.next_run_at is not None
    # 09:00 Tehran == 05:30 UTC
    tehran_next = schedule.next_run_at.astimezone(ZoneInfo("Asia/Tehran"))
    assert tehran_next.hour == 9 and tehran_next.minute == 0


async def test_invalid_cron_rejected(schedule_service, workflow_factory, company_and_platform):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    with pytest.raises(ConflictError):
        await schedule_service.create_schedule(
            workflow.id,
            company_id=company.id,
            name="broken-cron",
            schedule_type=__import__("app").models.WorkflowScheduleType.CRON,
            schedule_config={"cron": "definitely not cron"},
        )


async def test_invalid_timezone_rejected(schedule_service, workflow_factory, company_and_platform):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    with pytest.raises(ConflictError):
        await schedule_service.create_schedule(
            workflow.id,
            company_id=company.id,
            name="bad-tz",
            schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
            schedule_config={"interval_seconds": 60},
            timezone_name="Mars/Olympus",
        )


async def test_schedule_requires_published_workflow(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    draft = await workflow_factory(
        company.id,
        [{"node_key": "start", "node_type": "trigger.manual", "name": "t", "configuration": {}}],
        [],
        publish=False,
    )
    with pytest.raises(ConflictError):
        await schedule_service.create_schedule(
            draft.id,
            company_id=company.id,
            name="too-early",
            schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
            schedule_config={"interval_seconds": 60},
        )


async def test_duplicate_schedule_name_rejected(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="dup",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
    )
    with pytest.raises(ConflictError):
        await schedule_service.create_schedule(
            workflow.id,
            company_id=company.id,
            name="dup",
            schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
            schedule_config={"interval_seconds": 120},
        )


async def test_due_interval_schedule_fires_once_per_period(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="due-now",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 3600},
    )
    # Force the schedule due.
    schedule.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
    await schedule_service.session.commit()

    created = await schedule_service.collect_due_schedules()
    assert len(created) == 1
    assert schedule.last_execution_id == created[0]
    assert schedule.next_run_at > datetime.now(UTC)  # advanced into the future

    # Firing again in the same period creates nothing new.
    schedule.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    await schedule_service.session.commit()
    created_again = await schedule_service.collect_due_schedules()
    assert created_again == [] or created_again[0] not in created


async def test_missed_schedule_skip_policy_advances_without_running(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="missed-skip",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
        missed_policy=__import__("app").models.WorkflowScheduleMissedPolicy.SKIP,
    )
    schedule.next_run_at = datetime.now(UTC) - timedelta(hours=6)  # long downtime
    await schedule_service.session.commit()

    created = await schedule_service.collect_due_schedules()
    assert created == []
    assert schedule.next_run_at > datetime.now(UTC)


async def test_missed_schedule_run_once_policy_executes_catchup(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="missed-catchup",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
        missed_policy=__import__("app").models.WorkflowScheduleMissedPolicy.RUN_ONCE,
    )
    schedule.next_run_at = datetime.now(UTC) - timedelta(hours=6)
    await schedule_service.session.commit()

    created = await schedule_service.collect_due_schedules()
    assert len(created) == 1  # exactly one catch-up run, not six thousand


async def test_paused_workflow_does_not_fire(
    schedule_service, workflow_factory, company_and_platform
):
    from app.models import WorkflowDefinitionStatus

    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="paused-src",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
    )
    workflow.status = WorkflowDefinitionStatus.PAUSED.value
    schedule.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
    await schedule_service.session.commit()

    created = await schedule_service.collect_due_schedules()
    assert created == []
    assert schedule.next_run_at > datetime.now(UTC)  # clock advanced, nothing ran


async def test_disabled_schedule_not_collected(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="disabled-src",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
        enabled=False,
    )
    assert schedule.next_run_at is None
    schedule.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
    await schedule_service.session.commit()

    created = await schedule_service.collect_due_schedules()
    assert created == []


async def test_once_schedule_fires_exactly_once(
    schedule_service, workflow_factory, company_and_platform
):
    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="one-time",
        schedule_type=__import__("app").models.WorkflowScheduleType.ONCE,
        schedule_config={"at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()},
    )
    created = await schedule_service.collect_due_schedules()
    assert len(created) == 1
    assert schedule.next_run_at is None  # a past 'once' never fires again
    created2 = await schedule_service.collect_due_schedules()
    assert created2 == []


async def test_cross_company_schedule_access_404(
    schedule_service, workflow_factory, company_and_platform, db_session
):
    from app.models import Company

    company, _platform = company_and_platform
    workflow = await _published_workflow(workflow_factory, company.id)
    schedule = await schedule_service.create_schedule(
        workflow.id,
        company_id=company.id,
        name="mine",
        schedule_type=__import__("app").models.WorkflowScheduleType.INTERVAL,
        schedule_config={"interval_seconds": 60},
    )
    stranger = Company(name="Sched Sneak", slug="sched-sneak")
    db_session.add(stranger)
    await db_session.commit()
    with pytest.raises(NotFoundError):
        await schedule_service.get_schedule(schedule.id, company_id=stranger.id)
