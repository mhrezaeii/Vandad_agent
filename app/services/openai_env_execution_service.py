"""Budget and account for environment-configured OpenAI requests."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.providers.base import ProviderPermanentError
from app.integrations.providers.openai_env import OpenAIEnvError, openai_chat_completion
from app.services.provider_budget_service import ProviderBudgetService


class OpenAIEnvExecutionService:
    """Execute one environment-key OpenAI request under company and task budgets.

    The reservation is committed before the network request. Missing provider
    cost remains unknown and keeps the full reservation charged. Ambiguous
    transport/5xx outcomes are never retried automatically.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self,
        *,
        company_id: UUID,
        system: str,
        user: str,
        operation: str,
        idempotency_key: str,
        task_id: UUID | None = None,
        timeout_seconds: float | None = None,
        temperature: float | None = 0.2,
        max_tokens: int | None = None,
        json_mode: bool = True,
    ) -> dict[str, Any]:
        settings = get_settings()
        secret = settings.openai_api_key
        if secret is None or not secret.get_secret_value().strip():
            raise ProviderPermanentError(
                "OPENAI_API_KEY is not configured in the backend environment.",
                code="openai_key_missing",
            )
        model = settings.openai_model.strip()
        if not model:
            raise ProviderPermanentError(
                "OPENAI_MODEL is not configured in the backend environment.",
                code="openai_model_missing",
            )
        if not idempotency_key.strip():
            raise ProviderPermanentError(
                "An idempotency key is required for a budgeted OpenAI request.",
                code="openai_idempotency_key_missing",
            )

        budget = ProviderBudgetService(self.session)
        reservation = await budget.reserve(
            company_id,
            "llm_openai",
            operation,
            idempotency_key=idempotency_key,
            model=model,
            require_budget=True,
            task_id=task_id,
        )
        if reservation is None:
            raise ProviderPermanentError(
                "A company OpenAI budget is required before this request.",
                code="provider_budget_required",
            )
        # A crash after this commit leaves a charged reservation rather than
        # permitting an unaccounted or duplicate paid request.
        await self.session.commit()

        try:
            result = await openai_chat_completion(
                system=system,
                user=user,
                timeout_seconds=timeout_seconds,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        except OpenAIEnvError as error:
            if error.definitive_rejection:
                await budget.settle(
                    reservation,
                    usage={
                        "cost_usd": "0",
                        "currency": "USD",
                        "model": model,
                        "attempts": 1,
                    },
                )
            else:
                await budget.settle(
                    reservation,
                    error_code="openai_response_outcome_unknown",
                    usage={"model": model, "attempts": 1},
                )
            await self.session.commit()
            raise ProviderPermanentError(
                str(error),
                code=(
                    "openai_request_rejected"
                    if error.definitive_rejection
                    else "openai_response_outcome_unknown"
                ),
            ) from error
        except httpx.HTTPError as error:
            await budget.settle(
                reservation,
                error_code="openai_transport_outcome_unknown",
                usage={"model": model, "attempts": 1},
            )
            await self.session.commit()
            raise ProviderPermanentError(
                "OpenAI request outcome is unknown; automatic retry is blocked.",
                code="openai_transport_outcome_unknown",
            ) from error
        except Exception as error:
            await budget.settle(
                reservation,
                error_code="openai_execution_outcome_unknown",
                usage={"model": model, "attempts": 1},
            )
            await self.session.commit()
            raise ProviderPermanentError(
                "OpenAI request failed with an unknown outcome; automatic retry is blocked.",
                code="openai_execution_outcome_unknown",
            ) from error

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        await budget.settle(
            reservation,
            usage={
                **usage,
                "model": result.get("model") or model,
                "attempts": 1,
            },
        )
        await self.session.commit()
        return result
