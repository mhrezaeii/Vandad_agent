# Agent Execution Runtime

The agent task runtime is separate from the workflow graph runtime. Workflow jobs use the Redis-backed workflow worker. Agent tasks use a PostgreSQL transactional outbox so task creation and dispatch intent commit atomically.

## Runtime path

1. The tenant-scoped API validates the code-owned agent definition, capability and active company-agent grant.
2. Dispatch-ready tasks are created as `queued`; an `agent_task_outbox` row is inserted in the same transaction. Non-ready catalog entries remain `pending`.
3. The dedicated worker claims outbox rows with PostgreSQL `FOR UPDATE SKIP LOCKED`, bounded concurrency and a renewable lease.
4. The task service creates an immutable run attempt. The dispatcher resolves only code-registered handlers.
5. Heartbeats renew both the run and outbox lease only while the current worker still owns both records.
6. Completion/failure transitions verify active run state and worker ownership. A stale worker cannot overwrite a recovered attempt.
7. Stale-run recovery rechecks the heartbeat cutoff while holding task/run locks and creates a bounded retry when budget remains.
8. Cancellation is cooperative at handler safe boundaries. An in-flight provider request may not be interruptible once sent; its result is not committed after cancellation is observed.
9. Persisted error text is redacted. Shutdown drains work for a bounded interval; work exceeding the grace period is left for lease recovery.

## Executable handlers

- **Monitoring Agent**: public discovery, change detection, knowledge extraction and combined `platform.monitor`.
- **Content Agent**: scenario generation through the existing content scenario service.
- **Video Production Agent**: takes an approved scenario/storyboard video project and dispatches it through the unified AI video-provider workflow. It does not create or manage characters, brand characters or talking avatars. It requires a company provider budget before paid dispatch.
- Authenticated browser actions and autonomous publishing remain disabled. Video task dispatch is approval-gated at the agent-task level and rechecks project approval before starting the workflow.

## Local operation

```powershell
alembic upgrade head
docker compose up -d --build agent-worker
docker compose logs -f agent-worker
```

The API and agent worker must share PostgreSQL. Monitoring and Content tasks use the PostgreSQL outbox directly. The Video Production Agent additionally uses the optional Redis-backed workflow queue; if Redis is unavailable, video dispatch fails closed while non-video agent tasks continue. The video workflow consumes the approved scenario/storyboard and returns provider-generated video artifacts; no local avatar or character renderer is part of this runtime. Configure `AGENT_WORKER_CONCURRENCY`, `AGENT_WORKER_POLL_INTERVAL`, `AGENT_WORKER_LEASE_SECONDS` and `AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS`.

## Progress events and approvals

Task lifecycle transitions and handler progress are persisted in `agent_task_events`. Clients can read `GET /api/v1/agent-tasks/{task_id}/events?after=0` or connect to `GET /api/v1/agent-tasks/{task_id}/events/stream?after=0` for server-sent events. The stream is company-scoped; reconnect using the last event sequence. Video tasks created for the Video Production Agent remain `awaiting_approval` until `POST /api/v1/agent-tasks/{task_id}/approve` is called. The video handler independently verifies the ContentProject is already `approved`.

Company provider budgets are managed under `/api/v1/providers/budgets`. LLM calls reserve the configured per-operation ceiling before execution and settle only provider-reported USD cost. If cost is not returned, the reservation remains charged as `unknown`. Paid video dispatch requires a budget and records a conservative reservation; media-job settlement/reconciliation is still outstanding.

## Production gates

- Full CI against isolated PostgreSQL and Redis.
- Aggregate failure-injection coverage for process death, stale leases, duplicate deliveries, cancellation and worker handoff; new cancellation/duplicate/tenant-boundary cases have been added but await the aggregate run.
- Live provider smoke tests only by explicit opt-in, with a strict spend ceiling.
- Verify run history and persisted results; task acceptance alone is not execution success.
