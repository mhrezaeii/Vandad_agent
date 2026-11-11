# Architecture and implementation status

Last reviewed: 2026-11-11 (backend completion pass)

## Intended runtime shape

```text
Web UI (Next.js operator console — initial implementation)
        |
        v
FastAPI API / authentication / company scope
        |
        +--> Company & Platform Context
        |       +--> Connections
        |       +--> Platform Discovery
        |       +--> Source Documents
        |       +--> Platform Knowledge / Brand Context
        |
        +--> Agent API
        |       +--> Code-owned Agent Catalog
        |       +--> Company Agent Enablement
        |       +--> Agent Task API
        |                 |
        |                 v
        |           Agent Task Dispatcher [IMPLEMENTED]
        |                 |
        |                 +--> Monitoring Handler [IMPLEMENTED]
        |                 +--> Content Scenario Handler [IMPLEMENTED]
        |                 +--> Video Production Handler [IMPLEMENTED]
        |                 +--> Tool/Capability Router [IMPLEMENTED]
        |                 +--> Approval Policies [IMPLEMENTED]
        |
        +--> Workflow API / Workflow Engine
        |       +--> Workflow Versions and Validation
        |       +--> Redis Queue
        |       +--> Workflow Worker
        |
        +--> Provider Fabric
        |       +--> OpenAI-compatible LLM
        |       +--> Browser / Playwright
        |       +--> HeyGen
        |       +--> APIMaster scene video
        |       +--> Generic HTTP / Mock
        |
        +--> Content & Media Services
                +--> Brand Character
                +--> Talking Avatar (Piper -> Wav2Lip -> FFmpeg)
                +--> Scene Video (Storyboard -> Scene Clips -> Assembly)
                +--> Persian Subtitles / Branding
                +--> Artifact Storage

Shared infrastructure: PostgreSQL + Alembic, Redis, artifact storage, audit events.
```

## Boundaries that must stay explicit

### Workflow execution vs agent task execution

These are two different levels of abstraction:

- A **workflow execution** is a persisted run of a validated, versioned graph. The current worker claims workflow jobs from Redis and runs `WorkflowEngine`.
- An **agent task** is a company-scoped request to accomplish a capability-oriented outcome. It has an idempotency key, priority, attempt budget, cancellation state and run history.
- An agent task may eventually invoke one or more workflows/tools, but it must not be silently treated as a workflow execution without an explicit orchestration contract.

Current state: PostgreSQL outbox dispatch, a dedicated agent worker, a code-owned capability router, Monitoring, Content scenario and approval-gated Video Production handlers exist. Only agents with real registered handlers and verified end-to-end coverage may be dispatch-ready. Worker reliability hardening is being validated on the production-hardening branch.

### Provider adapters

Provider adapters are infrastructure, not product agents. They should expose stable capability contracts and return normalized results/errors/usage. Product services and agents should not hard-code vendor-specific HTTP details.

The OpenAI-compatible LLM adapter and APIMaster video adapter serve different APIs. APIMaster's current adapter is scene generation and explicitly does not support lip-sync. Do not route LLM requests to APIMaster unless a separately verified APIMaster LLM endpoint and adapter are added.

Provider routing should later support:
- explicit provider/model choice and a deterministic fallback policy;
- task constraints (language, modality, duration, aspect ratio, reference image, lip-sync);
- user/company budgets and maximum spend per operation;
- observed latency, failure rate, provider-reported usage and actual cost;
- an operator override for debugging and quality comparison.

Do not claim "cheapest" based on advertised prices alone. Compare real output quality, successful completion rate, retries, media duration/resolution and actual reported cost per accepted deliverable.

### Two video pipelines

Keep the two video paths separate:

1. **Talking avatar**: Persian speech generation, face/lip animation and final rendering. It is the path for a persistent presenter speaking a script.
2. **Scene video**: storyboard, independent scene generation, clip retrieval and assembly. It is the path for product footage, B-roll and cinematic scenes.

The Video Production Agent routes approved tasks into these existing workflows, but each pipeline retains its own capability contract. Scene generation is not a substitute for lip-sync.

## Current implementation map

| Area | Status | Notes |
|---|---|---|
| Tenant-scoped API and authorization | Implemented foundation | Continue tenant-isolation tests for every new route |
| Platform connections and credential boundary | Implemented foundation | Secrets must remain outside ordinary model/config output |
| Platform discovery and knowledge extraction | Implemented foundation / stacked PRs | Validate source provenance and refresh behavior |
| Brand and content workflows | Implemented foundation | Vandefence is the initial seeded brand, not a hard-coded tenant limit |
| Workflow graph runtime and worker | Implemented | Redis-backed workflow execution |
| Agent catalog and company enablement | Implemented on feature stack | Definitions are code-owned; company rows only enable approved agents |
| Agent task/run persistence and transactional outbox | Implemented on feature stack | Tenant-scoped lifecycle and idempotency |
| Agent dispatcher and dedicated worker | Implemented foundation | Monitoring, Content and approval-gated Video handlers; aggregate reliability/tenant E2E validation remains |
| Provider budgets and usage ledger | Implemented foundation | Monthly/per-operation company budgets, LLM usage settlement and paid video reservations; unknown costs remain reserved |
| Provider health/latency/error telemetry | Partial | LLM latency, attempts and usage are recorded; provider health history and reconciliation remain |
| Deterministic provider/model routing and fallback | Partial | Explicit connection/model selection exists; ordered fallback and cross-provider capability negotiation remain |
| APIMaster scene-video adapter | Implemented foundation | Real provider contract/output must be tested with credentials |
| Generic HTTP video adapter | Implemented foundation | Must be configured with strict URL/download allowlists |
| Talking-avatar local renderer | Utility script exists | Local runtime/model assets are not source-controlled |
| Web UI | Initial implementation | Persian RTL Next.js operator console under `web/`; visual/accessibility hardening and production identity/RBAC remain |

## Required security and reliability invariants

1. Every business record and query is scoped by company/tenant.
2. A task's agent key and capability must be validated against the code-owned catalog.
3. A company cannot activate an unknown agent or grant capabilities by editing its own payload.
4. Task creation must be idempotent when the caller supplies an idempotency key.
5. A task must not be reported as running/completed until a worker actually owns and executes a run.
6. Retries must respect attempt limits and distinguish transient from permanent provider failures.
7. Cancellation must be cooperative and checked at safe boundaries; paid operations need explicit idempotency.
8. Provider credentials must never appear in logs, task results, audit payloads or API errors.
9. Media download URLs must be validated against an allowlist and must not forward provider credentials to arbitrary redirect hosts.
10. Provider costs must be reported as unknown when the provider does not return reliable usage/cost data.
11. Alembic migration history is append-only once applied. Never rewrite or remove applied revisions.
12. The mock video provider must remain unavailable in production.

## Next implementation sequence

### P0 — Backend runtime contract

- [x] PostgreSQL transactional outbox and dedicated agent worker.
- [x] Code-owned handler registry and explicit capability dispatch.
- [x] Atomic task/run lifecycle transitions and bounded retries.
- [x] Worker ownership fencing on heartbeat and terminal transitions.
- [x] Cooperative cancellation checks at handler safe boundaries.
- [x] Stale-run recovery with heartbeat recheck and bounded retry.
- [x] Bounded graceful shutdown and persisted error redaction.
- [ ] Failure-injection and full CI validation before merge/deployment.
- [x] Durable task lifecycle/progress events, JSON event history and company-scoped SSE stream.
- [x] Explicit code-owned Tool/Capability Router for every registered handler/capability pair.
- [ ] Tenant-isolated end-to-end tests for every dispatch-ready agent.
- [ ] Aggregate failure-injection and full CI acceptance before merge/deployment.

### P1 — Provider and cost control (not complete)

- Normalize provider/model capabilities and constraints.
- [x] Add company/provider monthly budgets and per-operation reservations; unknown cost remains reserved rather than being reported as free.
- [x] Record LLM provider-reported usage, latency, attempts and actual cost when explicitly supplied.
- [x] Require a configured company budget before dispatching paid video-agent operations.
- [ ] Complete provider health/error history, operator reconciliation and explicit ordered fallback/capability routing.
- [ ] Add provider/model capability constraints for language, modality, duration, aspect ratio and lip-sync.
- Benchmark APIMaster, direct vendor APIs and other compatible gateways on accepted output quality and total cost.

Paid autonomous execution remains gated until these controls are implemented and verified.

### P2 — Product workflows

- [x] Monitoring task dispatch and public discovery / knowledge extraction handler.
- [x] Content scenario generation handler.
- [x] Human approval lifecycle for paid video tasks and project script/scenario approval before generation.
- [x] Video Production Agent routes approved avatar and scene projects to their separate workflow pipelines.
- [ ] Provider settlement callback from completed media jobs into the usage ledger; current paid-video reservation remains conservative until reconciliation.
- [ ] Mock-provider integration coverage and opt-in live smoke tests with spend ceilings.

### P3 — Web UI (initial implementation)

A separate Next.js + TypeScript frontend has been added under `web/` and consumes the public API. It is Persian-first, RTL and responsive. It is an initial operator console; visual polish, accessibility review, user authentication/RBAC and full production deployment remain before P3 acceptance.

Initial screens:
- Overview: task activity, recent runs, provider health and spend.
- Agent Center: catalog, activation state, capabilities and readiness.
- Task Center: create task, progress, cancel/retry and run timeline.
- Company Brain: platforms, sources, extracted knowledge and brand context.
- Content Studio: brand, script, approval, avatar/scene choice and rendered assets.
- Provider Center: provider/model configuration, health, latency, usage and budgets.
- Settings: company profile, members, credentials and audit trail.

Do not couple React components to internal database models. Treat the OpenAPI contract as the UI boundary.
