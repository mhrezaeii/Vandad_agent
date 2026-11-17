# LLM ordered fallback

LLM connections use the existing `llm_openai` adapter. An ordered fallback
chain is configured on the **primary** `PlatformConnection.configuration`
object with the `llm_fallbacks` key.

Example:

```json
{
  "url": "https://api.example.com/v1",
  "model": "primary-model",
  "llm_fallbacks": [
    {
      "model": "lower-cost-model"
    },
    {
      "connection_id": "00000000-0000-4000-8000-000000000001"
    },
    {
      "connection_id": "00000000-0000-4000-8000-000000000002",
      "model": "backup-model"
    }
  ]
}
```

Rules:

1. The primary connection is always tried first.
2. Fallback entries are tried in array order; at most five entries are allowed.
3. An entry with only `model` tries that model against the primary connection.
4. An entry with `connection_id` uses that connection's own endpoint, credential,
   and configured model unless `model` is explicitly supplied.
5. Every fallback connection must belong to the same company and use
   `llm_openai`. A missing, foreign-tenant, or incompatible connection is rejected.
6. Fallback candidates with inactive or unavailable credentials are skipped.
7. All attempts for one logical operation share one provider-budget reservation
   and one Agent Task spend ceiling.

## Billing and retry safety

Fallback is allowed only when the provider definitively rejected the request or
the connection failed before the request was sent (for example, a connection
failure, HTTP 401/404, or HTTP 429). A timeout after dispatch, an ambiguous
transport failure, or a server-side 5xx response stops the chain. The reservation
is retained as unknown usage for reconciliation; the workflow is not allowed
to retry that potentially billable operation automatically.

If every candidate returns a definitive rejection, the logical reservation is
settled at USD 0 and the operation fails with the final sanitized provider error.
No fallback call is made for malformed prompt data or an unexpected provider
response shape.

The `llm_fallbacks` key is internal routing metadata and is never sent to the
LLM endpoint. Configure connection IDs only after creating and validating the
target connection. Do not put API keys or other secrets in this configuration;
credentials remain in the existing credential vault.
