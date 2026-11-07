"""Provider execution with bounded timeouts and classified retries.

Rules enforced here:

- Every provider call is wrapped in ``asyncio.wait_for`` — a provider can
  never block the API beyond its time budget.
- Only transient failures are retried, only for idempotent operations.
- Permanent failures (e.g. invalid credentials) fail immediately.
- Backoff is exponential with a cap; injectable for fast tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.correlation import get_correlation_id
from app.integrations.providers.adapter import ProviderAdapter
from app.integrations.providers.base import (
    ProviderError,
    ProviderExecutionContext,
    ProviderPermanentError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderTransientError,
)

logger = logging.getLogger("app.providers")

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_base_seconds: float = 0.5
    backoff_cap_seconds: float = 8.0


@dataclass(frozen=True)
class ExecutionOutcome:
    result: ProviderResult | None
    error: ProviderError | None
    attempts: int

    @property
    def success(self) -> bool:
        return self.result is not None and self.result.success


def classify_exception(exc: Exception) -> ProviderError:
    """Map an arbitrary exception to a standardized, sanitized ProviderError."""
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, asyncio.TimeoutError):
        return ProviderTimeoutError("The provider operation timed out.")
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return ProviderTransientError(
            "The provider could not be reached.", code="provider_unreachable"
        )
    # Unknown failures are treated as permanent: fail safe, never retry blindly.
    return ProviderPermanentError(
        "The provider operation failed.", code="provider_failure"
    )


class ProviderExecutor:
    def __init__(self, *, default_policy: RetryPolicy, default_timeout_seconds: float) -> None:
        self._default_policy = default_policy
        self._default_timeout = default_timeout_seconds

    async def run(
        self,
        adapter: ProviderAdapter,
        context: ProviderExecutionContext,
        operation: Callable[[ProviderExecutionContext], Awaitable[ProviderResult]],
        *,
        idempotent: bool | None = None,
        policy: RetryPolicy | None = None,
    ) -> ExecutionOutcome:
        """Execute ``operation`` under timeout/retry rules; never raises.

        Failures surface as ``ExecutionOutcome.error`` with a sanitized message.
        """
        resolved_policy = policy or self._default_policy
        is_idempotent = context.configuration.get(
            "idempotent", adapter.default_operation_idempotent if idempotent is None else idempotent
        )
        attempts_allowed = resolved_policy.max_attempts if is_idempotent else 1

        last_error: ProviderError | None = None
        for attempt in range(1, attempts_allowed + 1):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(operation(context), timeout=context.timeout_seconds)
            except asyncio.TimeoutError:
                last_error = ProviderTimeoutError(
                    f"Provider operation timed out after {context.timeout_seconds:.1f}s."
                )
            except ProviderError as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001 - boundary converts everything
                last_error = classify_exception(exc)
            else:
                return ExecutionOutcome(result=result, error=None, attempts=attempt)

            # Permanent failures and exhausted budgets stop immediately.
            if not last_error.is_transient or attempt == attempts_allowed:
                break

            delay = min(
                resolved_policy.backoff_base_seconds * (2 ** (attempt - 1)),
                resolved_policy.backoff_cap_seconds,
            )
            logger.warning(
                "provider transient failure, retrying (attempt %d/%d, delay %.2fs): %s [%s]",
                attempt,
                attempts_allowed,
                delay,
                last_error.code,
                get_correlation_id() or "-",
            )
            await asyncio.sleep(delay)

        return ExecutionOutcome(result=None, error=last_error, attempts=attempts_allowed)

    def build_context(
        self,
        *,
        adapter: ProviderAdapter,
        connection_id: uuid.UUID,
        company_id: uuid.UUID,
        platform_id: uuid.UUID,
        configuration: dict,
        credential=None,  # SecretValue | None
        credential_type: str | None = None,
        secret_reference: str | None = None,
        operation: str = "validate_connection",
        timeout_seconds: float | None = None,
    ) -> ProviderExecutionContext:
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        return ProviderExecutionContext(
            provider_id=adapter.provider_id,
            connection_id=connection_id,
            company_id=company_id,
            platform_id=platform_id,
            configuration=configuration,
            credential=credential,
            credential_type=credential_type,
            secret_reference=secret_reference,
            timeout_seconds=max(0.1, min(timeout, 120.0)),
            correlation_id=get_correlation_id(),
            operation=operation,
        )
