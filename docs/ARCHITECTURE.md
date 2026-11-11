# Architecture and implementation status

Last reviewed: 2026-11-11

## Intended runtime shape

```text
Web UI (not implemented yet)
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
        |           Orchestrator / Dispatcher  [NOT IMPLEMENTED]
        |                 |
        |                 +--> Agent Handlers [NOT IMPLEMENTED]
        |                 +--> Tool/Capability Router [NOT IMPLEMENTED]
        |                 +--> Approval Policies [PARTIAL]
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

Current state: task creation and lifecycle persistence exist on the stacked agent-task branch, but dispatch is intentionally disabled. There is no real agent-task consumer, handler registry, planner or dispatcher in that branch. Keep `dispatch_ready=False` until an agent has an actual handler and the full execution path is covered by tests.

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

A future Video Production Agent can orchestrate both, but each pipeline retains its own capability contract. Scene generation is not a substitute for lip-sync.

## Current implementation map

| Area | Status | Notes |
|---|---|---|
| Tenant-scoped API and authorization | Implemented foundation | Continue tenant-isolation tests for every new route |
| Platform connections and credential boundary | Implemented foundation | Secrets must remain outside ordinary model/config output |
| Platform discovery and knowledge extraction | Implemented foundation / stacked PRs | Validate source provenance and refresh behavior |
| Brand and content workflows | Implemented foundation | Vandefence is the initial seeded brand, not a hard-coded tenant limit |
| Workflow graph runtime and worker | Implemented | This is the only verified background execution path in main |
| Agent catalog and company enablement | Stacked PR | Definitions are code-owned; company rows only enable approved agents |
| Agent task/run persistence | Stacked PR | Does not dispatch work yet |
| Agent orchestrator and handler execution | Not implemented | Highest backend architecture gap |
| APIMaster scene-video adapter | Implemented foundation | Real provider contract/output must be tested with credentials |
| Generic HTTP video adapter | Implemented foundation | Must be configured with strict URL/download allowlists |
| Talking-avatar local renderer | Utility script exists | Local runtime/model assets are not source-controlled |
| Polished web UI | Not implemented | Build only after task, run, provider and artifact API contracts stabilize |

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

### P0 — Complete the backend runtime contract

- Review and merge the existing platform-knowledge, brand-context, agent-registry and task/run work in dependency order, after CI and diff review.
- Define an explicit agent handler protocol: validated input, capability, tenant scope, cancellation context, progress/event reporting, normalized result and error.
- Add a durable dispatch mechanism from task creation to the worker. Do not rely on an in-process background task.
- Implement atomic task claiming and run creation so two workers cannot execute the same attempt.
- Add lease/heartbeat recovery, bounded concurrency, retry/backoff, cancellation and graceful shutdown.
- Connect agent handlers to the existing provider executor/workflow engine through an explicit orchestration interface.
- Keep all built-in agents non-dispatch-ready until their handlers are real and tested.

### P1 — Provider and cost control

- Normalize provider/model capabilities and constraints.
- Add provider health/latency/error telemetry and actual usage/cost recording.
- Add deterministic routing rules and fallback; no hidden model switching for paid calls.
- Add company budgets, per-task spend ceilings and approval gates for costly operations.
- Benchmark APIMaster, direct vendor APIs and other compatible gateways on accepted output quality and total cost.

### P2 — Product workflows

- Complete a single end-to-end Monitoring task.
- Complete a single end-to-end Content task that uses brand/platform knowledge and approval.
- Complete a Video task that chooses talking-avatar vs scene-video based on requested output.
- Add integration tests using mock providers and a small set of opt-in live smoke tests.

### P3 — Web UI

Create a separate frontend application (recommended: React/Next.js + TypeScript) consuming the stable API. The UI should be Persian-first with RTL support, responsive layouts and an accessible component system.

Initial screens:
- Overview: task activity, recent runs, provider health and spend.
- Agent Center: catalog, activation state, capabilities and readiness.
- Task Center: create task, progress, cancel/retry and run timeline.
- Company Brain: platforms, sources, extracted knowledge and brand context.
- Content Studio: brand, script, approval, avatar/scene choice and rendered assets.
- Provider Center: provider/model configuration, health, latency, usage and budgets.
- Settings: company profile, members, credentials and audit trail.

Do not couple React components to internal database models. Treat the OpenAPI contract as the UI boundary.
