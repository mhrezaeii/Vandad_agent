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
- Authenticated browser actions, autonomous publishing and paid video generation are not enabled by this change. They require explicit approval, budget and provider contracts before being marked dispatch-ready.

## Local operation

```powershell
alembic upgrade head
docker compose up -d --build agent-worker
docker compose logs -f agent-worker
```

The API and agent worker must share PostgreSQL. The agent worker does not require Redis. Configure `AGENT_WORKER_CONCURRENCY`, `AGENT_WORKER_POLL_INTERVAL`, `AGENT_WORKER_LEASE_SECONDS` and `AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS`.

## Production gates

- Full CI against isolated PostgreSQL and Redis.
- Failure-injection coverage for process death, stale leases, duplicate deliveries, cancellation and worker handoff.
- Live provider smoke tests only by explicit opt-in, with a strict spend ceiling.
- Verify run history and persisted results; task acceptance alone is not execution success.
