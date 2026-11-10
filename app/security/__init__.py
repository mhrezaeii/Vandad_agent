"""Security helpers: secret reference validation, redaction, crypto, authorization.

from app.security.redaction import redact_text, validate_secret_reference
from app.security.crypto import MasterKeyBox
from app.security.secret_value import SecretValue
from app.security.authorization import Principal, authorize_company_access
"""

from app.security.authorization import (
    AuthorizationError,
    CompanyScope,
    Principal,
    authorize_company_access,
    build_principal_from_settings,
)
from app.security.crypto import (
    AESGCM_KEY_SIZE_BYTES,
    CredentialAuthenticationError,
    CredentialCryptoError,
    MasterKeyBox,
    MasterKeyConfigurationError,
)
from app.security.redaction import (
    REDACTED_VALUE,
    is_valid_secret_reference,
    redact_mapping,
    redact_text,
    validate_secret_reference,
)
from app.security.secret_value import SecretValue

__all__ = [
    "AESGCM_KEY_SIZE_BYTES",
    "AuthorizationError",
    "CompanyScope",
    "CredentialAuthenticationError",
    "CredentialCryptoError",
    "MasterKeyBox",
    "MasterKeyConfigurationError",
    "Principal",
    "REDACTED_VALUE",
    "SecretValue",
    "authorize_company_access",
    "build_principal_from_settings",
    "is_valid_secret_reference",
    "redact_mapping",
    "redact_text",
    "validate_secret_reference",
]
