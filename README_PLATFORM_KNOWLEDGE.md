# Platform Knowledge Extraction

This feature adds the first AI knowledge layer after Platform Discovery.

## Flow

1. Register a company-owned platform and configure its public URL.
2. Discover its pages with `POST /api/v1/platforms/{platform_id}/discover`.
3. Extract structured knowledge with `POST /api/v1/platforms/{platform_id}/knowledge/extract`.
4. Read the latest snapshot with `GET /api/v1/platforms/{platform_id}/knowledge/latest`.

The extraction request accepts an existing, validated `llm_openai` connection belonging to the same company:

```json
{
  "connection_id": "00000000-0000-0000-0000-000000000000",
  "max_documents": 20
}
```

## Stored knowledge

Each extraction creates an immutable `platform_knowledge_snapshots` row containing:
- a source-document ID list and source fingerprint;
- a structured Persian summary;
- products and services, capabilities, workflows, audiences and differentiators;
- content opportunities and explicitly identified unknowns;
- the model identifier and extraction timestamp.

Every extracted fact must cite a URL from the discovered source set and include an evidence excerpt that is verified against that page's stored text. The page content is treated as untrusted reference material, not instructions. The extraction context is bounded to 80,000 characters.

## Operational notes

- Run Alembic migrations before using the new endpoints.
- The extraction endpoint invokes the configured text provider and may incur LLM usage charges.
- Discovery and extraction are currently explicit API operations. Scheduled monitoring and automatic scenario generation are follow-up work.
- This phase does not create or manage brand characters, avatars, voices or lip-sync.
