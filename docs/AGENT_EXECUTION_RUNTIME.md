# Agent Execution Runtime

The agent task runtime is separate from the workflow graph runtime. Workflow jobs continue to use the existing Redis queue and workflow worker. Agent tasks use a PostgreSQL transactional outbox so task creation/retry and dispatch intent commit in the same database transaction.

## Runtime path

1. The tenant-scoped API validates the built-in agent definition, capability and active company-agent grant.
2. Dispatch-ready tasks are created as `queued` and an `agent_task_outbox` row is inserted in the same transaction. Non-ready catalog entries remain `pending` and are not dispatched.
3. The dedicated worker claims outbox rows with `FOR UPDATE SKIP LOCKED`, a bounded concurrency semaphore and a renewable lease.
4. The task service creates an immutable run attempt. The dispatcher resolves a code-registered handler; database values cannot introduce executable code or capabilities.
5. The worker heartbeats the active run and outbox lease, persists result/error state, and schedules bounded retries with backoff.
6. Stale run leases are recovered into a failed attempt and a new outbox attempt, subject to the task's max-attempt limit.

## First executable agent

Only the Monitoring Agent is dispatch-ready in this phase:
- `platform.discover`: public-source discovery for a platform owned by the task's company.
- `platform.change.detect`: repeats public discovery and returns created/changed/unchanged document counts.
- `platform.knowledge.extract`: source-grounded extraction using a validated, company-owned `llm_openai` connection.

Authenticated browser operations are deliberately not enabled by this first handler. Browser Identity, Content and Video remain catalog-only until their handler contracts and safeguards are implemented. The existing talking-avatar and scene-video pipelines are unchanged.

## Local operation

Apply migrations and start the dedicated worker:

```powershell
alembic upgrade head
docker compose up -d --build agent-worker
docker compose logs -f agent-worker
```

The API service and agent worker must use the same PostgreSQL database. The agent worker does not depend on Redis. Configure `AGENT_WORKER_CONCURRENCY`, `AGENT_WORKER_POLL_INTERVAL`, and `AGENT_WORKER_LEASE_SECONDS` to tune polling and execution.

## Task payloads

For `platform.discover` or `platform.change.detect`:

```json
{
  "agent_key": "monitoring",
  "capability": "platform.discover",
  "title": "Discover platform sources",
  "input_payload": {
    "platform_id": "<company-owned-platform-uuid>",
    "max_pages": 10
  }
}
```

For `platform.knowledge.extract`, include `connection_id` referencing a validated LLM connection owned by the same company. The worker enforces tenant ownership again at execution time.
