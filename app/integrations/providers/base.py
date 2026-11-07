"""Provider contract: typed results, errors, execution context.

Everything crossing the provider boundary is sanitized by construction:
``ProviderResult.message`` is provider-authored and must never contain
exception payloads or secret material, and ``ProviderExecutionContext`` carries
credentials only as ``SecretValue``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.security.secret_value import SecretValue


class ProviderCapability(enum.StrEnum):
    CONNECTION_VALIDATION = "connection_validation"
    HEALTH_CHECK = "health_check"
    EXECUTION = "execution"


@dataclass(frozen=True)
class ProviderResult:
    """Sanitized outcome of a provider operation."""

    provider_id: str
    success: bool
    message: str
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, provider_id: str, message: str, *, metadata: dict[str, Any] | None = None) -> ProviderResult:
        return cls(provider_id=provider_id, success=False, message=message, metadata=metadata or {})


class ProviderError(Exception):
    """Standardized provider failure.

    ``kind`` drives retry classification: only ``transient`` failures are ever
    retried, and only for idempotent operations. The message must be safe for
    storage and display (no exception payloads, no secrets).
    """

    kind: str = "permanent"

    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        # Explicit retryable override wins over the class-level kind.
        if retryable is not None:
            self.kind = "transient" if retryable else "permanent"

    @property
    def is_transient(self) -> bool:
        return self.kind == "transient"


class ProviderTransientError(ProviderError):
    kind = "transient"


class ProviderPermanentError(ProviderError):
    kind = "permanent"


class InvalidCredentialError(ProviderPermanentError):
    """Credential rejected by the provider: never retried."""

    def __init__(self, message: str = "The credential was rejected by the provider.") -> None:
        super().__init__(message, code="invalid_credential")


class ProviderTimeoutError(ProviderTransientError):
    """The provider call exceeded its time budget."""

    def __init__(self, message: str, *, code: str = "provider_timeout") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True)
class ProviderExecutionContext:
    """Inputs for one provider operation.

    ``credential`` is the resolved ``SecretValue`` (or None for locator-only
    credentials); ``secret_reference`` is the locator metadata which is safe to
    log. Everything here is internal to the execution boundary.
    """

    provider_id: str
    connection_id: uuid.UUID
    company_id: uuid.UUID
    platform_id: uuid.UUID
    configuration: dict[str, Any]
    credential: SecretValue | None
    credential_type: str | None
    secret_reference: str | None
    timeout_seconds: float
    correlation_id: str | None = None
    operation: str = "validate_connection"

    def __repr__(self) -> str:  # guards against accidental secret exposure
        return (
            f"ProviderExecutionContext(provider_id={self.provider_id!r}, "
            f"connection_id={self.connection_id}, operation={self.operation!r}, "
            f"credential=<{'set' if self.credential else 'none'}>)"
        )
