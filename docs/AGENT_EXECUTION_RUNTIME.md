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
- **Content Agent**: scenario generation through the existing content scenario service, deterministic topic analysis (`content.analysis.select`) and the single-click `content.production.run` pipeline. The pipeline is backend-owned and runs monitoring → analysis → scene project → GPT scenario → quality review/approval → existing video workflow dispatch → bounded wait and QC verification. It emits durable progress events (`production.started` … `delivery.completed`), persists an `agent_task_id` marker in `scene_options` so a retry resumes instead of creating a second project or a second paid dispatch, and never re-submits a video job that is already in flight.
- **Video Production Agent**: accepts an approved scenario/storyboard video project and dispatches it through the KIE-only AI video-provider workflow. KIE provider-native audio is preserved in the final MP4. WaveSpeed and other video providers were intentionally removed by product decision and are not fallback targets. Character creation, persistent brand characters and talking-avatar production are out of scope. The KIE adapter, model-specific preflight checks, live credit-balance probe, durable submit fence and final MP4 audio/video validation are implemented. Credentialed end-to-end acceptance, output quality and supplier-account behavior remain unverified. Paid dispatch requires both a company provider budget and, when configured, the parent task spend ceiling.
- Authenticated browser actions and autonomous publishing remain disabled. Video task dispatch is approval-gated at the agent-task level and rechecks project approval before starting the workflow.

## Local operation

```powershell
alembic upgrade head
docker compose up -d --build agent-worker
docker compose logs -f agent-worker
```

The API and agent worker must share PostgreSQL. Monitoring and Content tasks use the PostgreSQL outbox directly. The Video Production Agent additionally uses the optional Redis-backed workflow queue; if Redis is unavailable, video dispatch fails closed while non-video agent tasks continue. The video workflow consumes the approved scenario/storyboard and returns provider-generated video artifacts. The KIE-only provider integration is implemented, but this document does not claim that a real production video has been generated or accepted. Configure `AGENT_WORKER_CONCURRENCY`, `AGENT_WORKER_POLL_INTERVAL`, `AGENT_WORKER_LEASE_SECONDS` and `AGENT_WORKER_SHUTDOWN_TIMEOUT_SECONDS`.

## Progress events and approvals

Task lifecycle transitions and handler progress are persisted in `agent_task_events`. Clients can read `GET /api/v1/agent-tasks/{task_id}/events?after=0` or connect to `GET /api/v1/agent-tasks/{task_id}/events/stream?after=0` for server-sent events. The stream is company-scoped; reconnect using the last event sequence. Video tasks created for the Video Production Agent remain `awaiting_approval` until `POST /api/v1/agent-tasks/{task_id}/approve` is called. The video handler independently verifies the ContentProject is already `approved`.

Company provider budgets are managed under `/api/v1/providers/budgets`. Each AgentTask may carry an optional `spend_ceiling_usd`. Every child LLM and video reservation receives the parent `task_id`; the identity is propagated through workflow `input_metadata.agent_task_id` into provider actions. `ProviderBudgetService` locks the parent task row while reserving or settling usage, so concurrent child operations across providers serialize against one aggregate task ceiling. The aggregate includes open reservations, settled actual USD cost and unknown-cost reservations. LLM operations reserve the configured per-operation ceiling and settle only provider-reported USD cost. KIE video usage settles only when all required provider job, composition and quality-review costs are known; otherwise the reservation remains charged for operator reconciliation. A company provider budget is still required independently of the task ceiling.

## Production gates

- Full CI against isolated PostgreSQL and Redis.
- Aggregate failure-injection coverage for process death, stale leases, duplicate deliveries, cancellation, worker handoff, cross-provider task-budget concurrency and tenant isolation must pass in CI before release sign-off.
- Live provider smoke tests only by explicit opt-in, with a strict spend ceiling.
- Verify run history and persisted results; task acceptance alone is not execution success.


## Spend ceiling and child-operation accounting

The optional task-level limit is set at task creation with `spend_ceiling_usd`.
It is distinct from each company's monthly and per-operation provider limits.
The parent task row is the serialization point: reservation and settlement lock
that row before reading or changing the task's aggregate usage. This prevents
two child operations (for example, GPT scenario generation and KIE video
generation) from independently spending against the same remaining balance.

A workflow launched by an agent carries `agent_task_id` in its
`input_metadata`. LLM provider nodes and content scenario nodes recover that
identifier and pass it to the same budget service. A video task also binds its
scene-video project reservation to the parent task; downstream KIE usage is
settled against that reservation. Unknown provider cost is never treated as
zero: the reservation remains charged until an operator reconciles it.

The code and tests cover reservation aggregation, settlement-aware accounting,
tenant scoping, concurrent reservations across different providers, and
workflow LLM propagation. Production staging and credentialed KIE execution
remain separate acceptance gates.
