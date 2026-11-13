"""Budgeted OpenAI-compatible LLM execution with explicit ordered fallback.

Fallback candidates are configured on the primary PlatformConnection under
the llm_fallbacks key. Only failures known to have been rejected before billing
are eligible for fallback; timeouts and server/transport failures remain
ambiguous and stop the route to avoid duplicate paid requests.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError
from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.integrations.providers.base import (
    ProviderError,
    ProviderPermanentError,
    ProviderResult,
)
from app.models import PlatformConnection
from app.security.crypto import MasterKeyBox
from app.services.credential_service import CredentialService
from app.services.provider_budget_service import ProviderBudgetService

_READY_STATUSES = frozenset({"connected", "configured", "degraded"})
_SAFE_HTTP_FALLBACK_STATUSES = frozenset({400, 401, 403, 404, 405, 413, 415, 422})
_MAX_FALLBACKS = 5


@dataclass(frozen=True, slots=True)
class _LlmCandidate:
    connection_id: uuid.UUID
    platform_id: uuid.UUID
    model: str
    configuration: dict[str, Any]
    credential: Any
    credential_type: str | None
    secret_reference: str | None


class LlmExecutionService:
    """Resolve a tenant-scoped route, reserve once, and execute candidates in order."""

    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self.master_key_box = master_key_box

    async def execute(
        self,
        *,
        company_id: uuid.UUID,
        connection_id: uuid.UUID,
        configuration_overrides: dict[str, Any] | None,
        timeout_seconds: float,
        operation: str,
        idempotency_key: str,
        task_id: uuid.UUID | None = None,
    ) -> ProviderResult:
        """Execute one logical LLM operation, falling back only after safe rejections."""
        primary = await self._get_connection(connection_id, company_id)
        if primary.provider != "llm_openai":
            raise ProviderPermanentError(
                "The selected connection is not an OpenAI-compatible LLM connection.",
                code="llm_connection_provider_invalid",
            )

        candidates = await self._build_candidates(
            primary, company_id, configuration_overrides or {}
        )
        if not candidates:
            raise ProviderPermanentError(
                "No configured LLM route candidate is ready for execution.",
                code="llm_route_unavailable",
            )

        adapter = self.registry.get("llm_openai")
        reservation = await ProviderBudgetService(self.session).reserve(
            company_id,
            adapter.provider_id,
            operation,
            idempotency_key=idempotency_key,
            model=candidates[0].model,
            require_budget=True,
            task_id=task_id,
        )
        # Persist the logical-operation reservation before crossing any provider
        # boundary. All fallback attempts share this one spend reservation.
        await self.session.commit()

        attempts = 0
        last_safe_error: ProviderError | None = None
        for candidate_index, candidate in enumerate(candidates):
            context = self.executor.build_context(
                adapter=adapter,
                connection_id=candidate.connection_id,
                company_id=company_id,
                platform_id=candidate.platform_id,
                configuration=candidate.configuration,
                credential=candidate.credential,
                credential_type=candidate.credential_type,
                secret_reference=candidate.secret_reference,
                operation="execute",
                timeout_seconds=timeout_seconds,
            )
            outcome = await self.executor.run(
                adapter, context, adapter.execute, idempotent=False
            )
            attempts += outcome.attempts
            if outcome.success and outcome.result is not None:
                result = outcome.result
                metadata = result.metadata or {}
                usage = {
                    **metadata,
                    **(metadata.get("usage") if isinstance(metadata.get("usage"), dict) else {}),
                }
                await ProviderBudgetService(self.session).settle(
                    reservation,
                    usage={
                        **usage,
                        "model": metadata.get("model") or candidate.model,
                        "latency_ms": result.latency_ms,
                        "attempts": attempts,
                    },
                )
                await self.session.commit()
                return ProviderResult(
                    provider_id=result.provider_id,
                    success=result.success,
                    message=result.message,
                    latency_ms=result.latency_ms,
                    metadata={
                        **metadata,
                        "routing_attempts": attempts,
                        "fallback_used": candidate_index > 0,
                    },
                )

            error = outcome.error or ProviderError(
                "The LLM provider operation failed.", code="llm_failure"
            )
            if self._safe_to_fallback(error):
                last_safe_error = error
                continue

            await ProviderBudgetService(self.session).settle(
                reservation,
                error_code=error.code or "llm_failure",
                usage={"attempts": attempts},
            )
            await self.session.commit()
            # Do not let the workflow engine retry an ambiguous paid request.
            raise ProviderPermanentError(
                error.message, code=error.code or "llm_failure"
            ) from error

        # Every attempted candidate returned a definitive rejection or failed
        # before connecting, so no provider accepted this logical request.
        await ProviderBudgetService(self.session).settle(
            reservation,
            usage={
                "cost_usd": "0",
                "currency": "USD",
                "attempts": attempts,
                "model": candidates[-1].model,
            },
        )
        await self.session.commit()
        raise ProviderPermanentError(
            (
                last_safe_error.message
                if last_safe_error is not None
                else "Every configured LLM route candidate rejected the request."
            ),
            code=(
                last_safe_error.code
                if last_safe_error is not None
                else "llm_fallback_exhausted"
            ),
        ) from last_safe_error

    async def _get_connection(
        self, connection_id: uuid.UUID, company_id: uuid.UUID
    ) -> PlatformConnection:
        connection = await self.session.scalar(
            select(PlatformConnection).where(
                PlatformConnection.id == connection_id,
                PlatformConnection.company_id == company_id,
            )
        )
        if connection is None:
            raise NotFoundError("LLM connection not found for this company.")
        return connection

    async def _build_candidates(
        self,
        primary: PlatformConnection,
        company_id: uuid.UUID,
        overrides: dict[str, Any],
    ) -> list[_LlmCandidate]:
        raw_fallbacks = (primary.configuration or {}).get("llm_fallbacks", [])
        if raw_fallbacks is None:
            raw_fallbacks = []
        if not isinstance(raw_fallbacks, list) or len(raw_fallbacks) > _MAX_FALLBACKS:
            raise ProviderPermanentError(
                f"llm_fallbacks must be a list with at most {_MAX_FALLBACKS} entries.",
                code="llm_fallback_config_invalid",
            )

        specs: list[tuple[PlatformConnection, str | None, bool]] = [
            (primary, None, True)
        ]
        for item in raw_fallbacks:
            if not isinstance(item, dict) or set(item) - {"connection_id", "model"}:
                raise ProviderPermanentError(
                    "Each LLM fallback must contain only connection_id and model.",
                    code="llm_fallback_config_invalid",
                )
            raw_connection_id = item.get("connection_id")
            try:
                candidate_id = (
                    uuid.UUID(str(raw_connection_id)) if raw_connection_id else primary.id
                )
            except (TypeError, ValueError) as exc:
                raise ProviderPermanentError(
                    "An LLM fallback contains an invalid connection_id.",
                    code="llm_fallback_config_invalid",
                ) from exc

            candidate = (
                primary
                if candidate_id == primary.id
                else await self._get_connection(candidate_id, company_id)
            )
            if candidate.provider != "llm_openai":
                raise ProviderPermanentError(
                    "Every LLM fallback connection must use llm_openai.",
                    code="llm_fallback_provider_invalid",
                )
            model_override = item.get("model")
            if model_override is not None and (
                not isinstance(model_override, str)
                or not model_override.strip()
                or len(model_override) > 200
            ):
                raise ProviderPermanentError(
                    "An LLM fallback model must be a non-empty string of at most 200 characters.",
                    code="llm_fallback_config_invalid",
                )
            specs.append((candidate, model_override, False))

        result: list[_LlmCandidate] = []
        seen: set[tuple[uuid.UUID, str]] = set()
        for connection, model_override, is_primary in specs:
            if connection.status not in _READY_STATUSES:
                continue
            base_configuration = dict(connection.configuration or {})
            configured_model = base_configuration.get("model")
            override_model = overrides.get("model") if is_primary else None
            model = model_override or override_model or configured_model
            if not isinstance(model, str) or not model.strip():
                # A misconfigured primary must not prevent a later, valid
                # explicitly configured fallback from being considered.
                continue
            identity = (connection.id, model)
            if identity in seen:
                continue
            seen.add(identity)

            # Route controls are internal metadata, never provider request
            # configuration. A fallback connection retains its own endpoint
            # and model unless its entry explicitly overrides the model.
            base_configuration.pop("llm_fallbacks", None)
            safe_overrides = dict(overrides)
            safe_overrides.pop("llm_fallbacks", None)
            if not is_primary:
                safe_overrides.pop("model", None)
            configuration = {**base_configuration, **safe_overrides}
            configuration["model"] = model

            credential = credential_type = secret_reference = None
            if connection.credential_reference_id is not None:
                try:
                    resolved = await CredentialService(
                        self.session, self.master_key_box
                    ).resolve_credential(
                        connection.credential_reference_id, company_id=company_id
                    )
                except (ConflictError, ForbiddenError, NotFoundError):
                    # A broken credential makes only this candidate unusable;
                    # the next explicitly configured tenant-owned candidate
                    # may still be healthy.
                    continue
                credential = resolved.payload
                credential_type = resolved.credential_type
                secret_reference = resolved.secret_reference

            result.append(
                _LlmCandidate(
                    connection_id=connection.id,
                    platform_id=connection.platform_id,
                    model=model,
                    configuration=configuration,
                    credential=credential,
                    credential_type=credential_type,
                    secret_reference=secret_reference,
                )
            )
        return result

    @staticmethod
    def _safe_to_fallback(error: ProviderError) -> bool:
        if error.code in {
            "provider_unreachable",
            "llm_rate_limited",
            "invalid_base_url",
            "invalid_configuration",
        }:
            return True
        if error.code.startswith("llm_http_"):
            try:
                status = int(error.code.removeprefix("llm_http_"))
            except ValueError:
                return False
            return status in _SAFE_HTTP_FALLBACK_STATUSES
        return False
