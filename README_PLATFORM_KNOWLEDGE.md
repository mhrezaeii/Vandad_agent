# Platform Knowledge Extraction

Knowledge extraction is **deterministic and LLM-free**. It converts persisted
source documents into a source-grounded `PlatformKnowledgePayload`; no model is
called, no provider connection is required and there is no usage charge.

## Source types

| Mode | Source | How it is configured |
|---|---|---|
| `public` | Public website (HTML, robots.txt, sitemap) | `platform.base_url` |
| `internal` | Admin-configured internal/staging environment | `configuration.knowledge_sources.internal_url` |
| `openapi` | OpenAPI/Swagger JSON | `configuration.knowledge_sources.docs_url` (+ `docs_mode: "openapi"`) |
| `document` | Markdown/text product documentation | `configuration.knowledge_sources.docs_url` (+ `docs_mode: "document"`) |
| `browser` | Authenticated browser snapshot | Requires a validated connection |

Private network destinations are only reachable in `internal` mode and only
when explicitly configured by an administrator; discovered links can never
widen that scope. All adapters are HTTP(S)-only, same-origin and size-bounded.

## Flow

1. Register a company-owned platform and configure its source URLs.
2. Discover pages with `POST /api/v1/platforms/{platform_id}/discover`
   (`{"mode": "internal"|"openapi"|"document"|"public"}`).
3. Extract knowledge with `POST /api/v1/platforms/{platform_id}/knowledge/extract`
   (body may be `{}`).
4. Read the latest snapshot with `GET /api/v1/platforms/{platform_id}/knowledge/latest`.
5. Refresh **all** platforms of a company with
   `POST /api/v1/content/companies/{company_id}/refresh`. The worker also runs
   `run_cycle()` every `PLATFORM_MONITOR_INTERVAL_SECONDS` (6 h by default).

## Deterministic extraction rules

- Structural signals are collected during crawling: navigation items, headings,
  lists, form labels, FAQ pairs and meta descriptions (stored in
  `metadata_json.signals`). OpenAPI paths/operations and markdown headings are
  parsed directly from the document.
- Signals are mapped to fact groups by explicit role and Persian/English keyword
  rules. Confidence is computed from signal authority plus source multiplicity.
- Every `evidence` string is copied verbatim from the cited document and is
  re-validated against the stored text before the snapshot is written. A fact
  without a supporting segment is never invented.
- Groups without supporting evidence stay empty and produce an explicit
  Persian `unknown`. A 404 page therefore yields unknowns, not claims.

## Stored knowledge

Each extraction creates an immutable `platform_knowledge_snapshots` row with a
source-document ID list, source fingerprint, Persian summary, five fact groups,
content opportunities, unknowns, the extractor version (`deterministic-v1`) and
the extraction timestamp.

## Operational notes

- Brand memory and the one-page flow read these snapshots directly; no refresh
  with a provider connection is needed.
- Scenario generation remains a separate, billable and approval-gated step.
- This phase does not create or manage brand characters, avatars, voices or lip-sync.
