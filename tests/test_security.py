"""Security helpers: secret reference validation and redaction."""

from __future__ import annotations

import pytest

from app.security.redaction import (
    REDACTED_VALUE,
    is_valid_secret_reference,
    redact_mapping,
    redact_text,
    validate_secret_reference,
)


@pytest.mark.parametrize(
    "reference",
    [
        "vault://platform-x/api-key",
        "vault://prod/github/oauth-token",
        "env:PLATFORM_X_TOKEN",
        "secret-store/production/admin-credentials",
        "aws-secretsmanager:prod/platform-x",
    ],
)
def test_valid_secret_references(reference: str):
    assert is_valid_secret_reference(reference)
    assert validate_secret_reference(reference) == reference


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "ab",  # too short
        "has space",
        "with\nnewline",
        " semi;colon leading space",
        "-leading-hyphen",
        "unicode-密码-reference",
    ],
)
def test_invalid_secret_references(reference: str):
    assert not is_valid_secret_reference(reference)
    with pytest.raises(ValueError):
        validate_secret_reference(reference)


def test_validate_reference_error_message_does_not_echo_value():
    # An invalid-format value must not be echoed back in the error text.
    with pytest.raises(ValueError) as excinfo:
        validate_secret_reference("raw secret hunter2 with spaces")
    assert "raw secret hunter2" not in str(excinfo.value)


def test_redact_mapping_masks_sensitive_keys():
    data = {
        "api_key": "super-secret",
        "nested": {"password": "hunter2", "plain": "value"},
        "oauth_token": "t0k3n",
        "normal_field": 42,
        "list": [{"secret_sauce": "x"}, {"name": "ok"}],
    }
    redacted = redact_mapping(data)
    assert redacted["api_key"] == REDACTED_VALUE
    assert redacted["nested"]["password"] == REDACTED_VALUE
    assert redacted["nested"]["plain"] == "value"
    assert redacted["oauth_token"] == REDACTED_VALUE
    assert redacted["normal_field"] == 42
    assert redacted["list"][0]["secret_sauce"] == REDACTED_VALUE
    assert redacted["list"][1]["name"] == "ok"
    # original untouched
    assert data["api_key"] == "super-secret"


def test_redact_text_masks_inline_secrets():
    text = "auth failed for api_key=hunter2 and password: topsecret; retrying"
    redacted = redact_text(text)
    assert "hunter2" not in redacted
    assert "topsecret" not in redacted
    assert REDACTED_VALUE in redacted


def test_redact_text_leaves_normal_text():
    assert redact_text("database check ok in 2.31ms") == "database check ok in 2.31ms"
