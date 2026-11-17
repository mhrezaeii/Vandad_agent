from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

import app.integrations.providers.openai_env as openai_env
from app.core.config import Settings


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict[str, Any]:
        return {
            "model": "gpt-6-luna",
            "choices": [{"message": {"content": "{\"ok\": true}"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        }


class _Client:
    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout
        self.request: dict[str, Any] | None = None

    async def __aenter__(self) -> _Client:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _Response:
        self.request = {"url": url, "headers": headers, "json": json}
        _requests.append(self.request)
        return _Response()


_requests: list[dict[str, Any]] = []


@pytest.mark.parametrize(
    ("reasoning_effort", "temperature_expected"),
    [("none", 0.1), ("medium", None)],
)
async def test_chat_completion_uses_model_compatible_reasoning_parameters(
    monkeypatch: pytest.MonkeyPatch,
    reasoning_effort: str,
    temperature_expected: float | None,
) -> None:
    _requests.clear()
    settings = Settings(
        openai_api_key=SecretStr("test-secret"),
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-6-luna",
        openai_reasoning_effort=reasoning_effort,
        openai_timeout_seconds=30,
        openai_max_completion_tokens=1200,
        _env_file=None,
    )
    monkeypatch.setattr(openai_env, "get_settings", lambda: settings)
    monkeypatch.setattr(openai_env.httpx, "AsyncClient", _Client)

    result = await openai_env.openai_chat_completion(
        system="Return JSON.",
        user="Return an object.",
        temperature=temperature_expected if temperature_expected is not None else 0.4,
        max_tokens=500,
    )

    payload = _requests[-1]["json"]
    assert payload["reasoning_effort"] == reasoning_effort
    assert payload["max_completion_tokens"] == 500
    if temperature_expected is None:
        assert "temperature" not in payload
    else:
        assert payload["temperature"] == temperature_expected
    assert result["content"] == "{\"ok\": true}"
    assert result["usage"]["total_tokens"] == 16
