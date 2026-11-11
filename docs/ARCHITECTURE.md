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
                +--> AI-native media production (provider generation/editing/composition)
                +--> Provider-returned final artifacts
                +--> Tenant-scoped artifact storage
                +--> Artifact Storage

Shared infrastructure: PostgreSQL + Alembic, Redis, artifact storage, audit events.
```

## Boundaries that must stay explicit

### Workflow execution vs agent task execution

These are two different levels of abstraction:

- A **workflow execution** is a persisted run of a validated, versioned graph. The current worker claims workflow jobs from Redis and runs `WorkflowEngine`.
- An **agent task** is a company-scoped request to accomplish a capability-oriented outcome. It has an idempotency key, priority, attempt budget, cancellation state and run history.
- An agent task may eventually invoke one or more workflows/tools, but it must not be silently treated as a workflow execution without an explicit orchestration contract.

Current state: PostgreSQL outbox dispatch, a dedicated agent worker, a code-owned capability router, Monitoring, Content scenario and approval-gated Video Production handlers exist. Only agents with real registered handlers and verified end-to-end coverage may be dispatch-ready. Worker reliability, durable progress events, reconnectable SSE, and code-owned capability input contracts are implemented on `fix/phase1-agent-runtime-failure-injection`; aggregate CI validation remains pending.

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

### Unified AI-native video production

There is one production path. Talking-avatar, presenter, scene-based, motion-graphics and cinematic showcase are styles within that path, not separate local rendering pipelines.

The orchestrator selects provider capabilities for generation, speech, lip-sync, editing and composition. A provider must explicitly advertise a capability before it is selected. If no configured provider can complete a required stage, execution fails closed; Vandad must not silently substitute FFmpeg, local TTS, local lip-sync or a local scene assembler. Provider-returned final artifacts are validated and stored without unnecessary local rendering.

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
| Provider budgets and usage ledger | Implemented foundation | Monthly/per-operation company budgets, per-task aggregate spend ceilings across agent handlers and child workflows, LLM usage settlement and paid video reservations; unknown costs remain reserved; GitHub Actions backend lint/tests and frontend lint/build passed on the current implementation |
| Provider health/latency/error telemetry | Implemented foundation | Tenant-scoped rolling 30-day request/error/latency metrics plus a tenant-scoped live connection health-probe endpoint; real vendor probes still require configured credentials |
| Deterministic provider/model routing and fallback | Partial | Ordered scene-video provider fallback now filters by composition, lip-sync and reference-image capability; configured video model catalog exposes known flags; LLM fallback and unknown model constraints remain |
| APIMaster scene-video adapter | Implemented foundation | Real provider contract/output must be tested with credentials |
| Generic HTTP video adapter | Implemented foundation | Must be configured with strict URL/download allowlists |
| AI-native video generation and composition | Implemented foundation | Provider-side composition is capability-gated; real credentialed output validation remains |
| Web UI and identity | Initial implementation | Persian RTL Next.js console, live task SSE, trusted OIDC-proxy identity, tenant claims and baseline roles; visual/accessibility review and deployment integration remain |

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
- [x] Tenant-boundary tests for Monitoring, Content and Video handlers; full worker-level E2E matrix still requires aggregate CI validation.
- [ ] Aggregate failure-injection and full CI acceptance before merge/deployment.

### P1 — Provider and cost control (not complete)

- [x] Add an optional per-agent-task spend ceiling and atomically aggregate reservations across child provider operations and workflows by locking the parent task row; implementation and CI verified; production staging validation remains pending.
- [x] Propagate task identity into Monitoring knowledge extraction, Content scenario generation, scene-video project reservations and avatar workflow provider reservations; CI verified; production staging validation remains pending.
- [x] Add a configured scene-video model capability catalog for known flags; unknown language, aspect-ratio and duration constraints remain explicitly unset until verified from provider contracts.
- [x] Add company/provider monthly budgets and per-operation reservations; unknown cost remains reserved rather than being reported as free.
- [x] Record LLM provider-reported usage, latency, attempts and actual cost when explicitly supplied.
- [x] Require a configured company budget before dispatching paid video-agent operations.
- [x] Add tenant-scoped rolling provider request/error/latency telemetry and admin-only manual spend reconciliation.
- [x] Add deterministic ordered scene-video fallback with composition/lip-sync/reference-image capability filtering, plus a tenant-scoped live connection health-probe API; broader LLM fallback remains.
- [ ] Populate verified model constraints for language, modality, duration and aspect ratio; do not infer undocumented provider limits.
- Benchmark APIMaster, direct vendor APIs and other compatible gateways on accepted output quality and total cost.

Paid autonomous execution remains gated until staging and credentialed end-to-end tests verify the task ceiling/context propagation, provider routing and live probes against real vendor behavior.

### P2 — Product workflows

- [x] Monitoring task dispatch and public discovery / knowledge extraction handler.
- [x] Content scenario generation handler.
- [x] Human approval lifecycle for paid video tasks and project script/scenario approval before generation.
- [x] Video Production Agent routes approved avatar and scene projects to their separate workflow pipelines.
- [x] Settle scene-video reservations automatically when every completed scene reports an explicit USD cost; otherwise keep the reservation for operator reconciliation.
- [ ] Mock-provider integration coverage and opt-in live smoke tests with spend ceilings.

### P3 — Web UI (initial implementation)

A separate Next.js + TypeScript frontend has been added under `web/` and consumes the API. It is Persian-first, RTL and responsive, with live task event streaming. Production requests now require a trusted OIDC/auth proxy to inject verified user, company-membership and role claims using a shared secret; viewer is read-only, operator can execute, and owner/admin control agent activation and budgets. The reverse proxy must strip inbound `X-Auth-*` headers before injecting trusted claims. Visual polish, accessibility review and deployment integration remain before P3 acceptance.

Initial screens:
- Overview: task activity, recent runs, provider health and spend.
- Agent Center: catalog, activation state, capabilities and readiness.
- Task Center: create task, progress, cancel/retry and run timeline.
- Company Brain: platforms, sources, extracted knowledge and brand context.
- Content Studio: brand, script, approval, avatar/scene choice and rendered assets.
- Provider Center: provider/model configuration, health, latency, usage and budgets.
- Settings: company profile, members, credentials and audit trail.

Do not couple React components to internal database models. Treat the OpenAPI contract as the UI boundary.
