"""HttpEndpointProvider: real HTTP reachability provider.

Validates that a configured URL is reachable and answers with an expected
status. Uses httpx with the connection's timeout; classifies outcomes:

- 2xx                     -> success
- 429 / 5xx / network     -> transient (retryable)
- other 4xx               -> permanent (e.g. invalid credential / bad URL)

Only status codes and timing are ever reported — response bodies are never
included in results, so nothing secret can leak.
"""

from __future__ import annotations

import httpx

from app.integrations.providers.adapter import ProviderAdapter, ProviderCredentialRequirement
from app.integrations.providers.base import (
    ProviderCapability,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
    ProviderTransientError,
)


class HttpEndpointProvider(ProviderAdapter):
    provider_id = "http_endpoint"
    display_name = "HTTP Endpoint"
    description = "Checks reachability of a configured HTTP(S) endpoint."
    capabilities = frozenset({ProviderCapability.CONNECTION_VALIDATION, ProviderCapability.HEALTH_CHECK})
    credential_requirements = (
        ProviderCredentialRequirement("api_key", required=False, description="Sent as bearer token when set."),
        ProviderCredentialRequirement("token", required=False, description="Sent as bearer token when set."),
    )

    async def validate_connection(self, context: ProviderExecutionContext) -> ProviderResult:
        url = context.configuration.get("url")
        if not url or not str(url).lower().startswith(("http://", "https://")):
            raise ProviderPermanentError(
                "Configuration must contain an 'url' key with an http(s) URL.",
                code="invalid_configuration",
            )
        expected_status = int(context.configuration.get("expected_status", 200))
        method = str(context.configuration.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            raise ProviderPermanentError(
                "Only GET and HEAD methods are supported.", code="invalid_configuration"
            )

        headers: dict[str, str] = {}
        if context.credential is not None and not context.credential.is_empty():
            headers["Authorization"] = f"Bearer {context.credential.reveal()}"

        import time

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=context.timeout_seconds, follow_redirects=True) as client:
                response = await client.request(method, str(url), headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                f"Endpoint did not respond within {context.timeout_seconds:.1f}s.",
                code="provider_timeout",
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                "Endpoint could not be reached.", code="provider_unreachable"
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        status = response.status_code

        if status == expected_status:
            return ProviderResult(
                provider_id=self.provider_id,
                success=True,
                message=f"Endpoint responded with status {status}.",
                latency_ms=latency_ms,
                metadata={"status": status, "url": str(url)},
            )
        if status == 429 or 500 <= status <= 599:
            raise ProviderTransientError(
                f"Endpoint responded with status {status}.", code="provider_server_error"
            )
        if 400 <= status <= 499:
            raise ProviderPermanentError(
                f"Endpoint responded with status {status}.", code="provider_client_error"
            )
        raise ProviderTransientError(
            f"Endpoint responded with unexpected status {status}.", code="provider_unexpected_status"
        )
