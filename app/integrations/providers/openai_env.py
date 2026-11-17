"""Server-side OpenAI Chat Completions client configured only from environment."""
from __future__ import annotations
from typing import Any
import httpx
from app.core.config import get_settings

class OpenAIEnvError(RuntimeError):
    """Sanitized OpenAI request failure; never includes credentials or raw bodies."""

async def openai_chat_completion(*, system: str, user: str, timeout_seconds: float | None = None,
    temperature: float | None = 0.2, max_tokens: int | None = None, json_mode: bool = True
) -> dict[str, Any]:
    settings = get_settings()
    secret = settings.openai_api_key
    api_key = secret.get_secret_value().strip() if secret else ""
    if not api_key:
        raise OpenAIEnvError("OPENAI_API_KEY is not configured in the backend .env file.")
    base_url = settings.openai_base_url.rstrip("/")
    model = settings.openai_model.strip()
    if not model:
        raise OpenAIEnvError("OPENAI_MODEL must be configured in the backend .env file.")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_completion_tokens": max_tokens or settings.openai_max_completion_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_seconds or settings.openai_timeout_seconds) as client:
        response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if response.status_code == 400 and "max_completion_tokens" in response.text:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
            response = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
    if response.status_code == 401:
        raise OpenAIEnvError("OpenAI rejected OPENAI_API_KEY. Check the key in backend .env.")
    if response.status_code == 429:
        raise OpenAIEnvError("OpenAI rate limit or quota reached. Check billing and usage limits.")
    if response.status_code >= 500:
        raise OpenAIEnvError(f"OpenAI service returned HTTP {response.status_code}.")
    if response.status_code >= 400:
        raise OpenAIEnvError(f"OpenAI rejected the request (HTTP {response.status_code}).")
    try:
        body = response.json()
        message = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OpenAIEnvError("OpenAI returned an unexpected Chat Completions response.") from exc
    if isinstance(message, list):
        message = "".join(str(part.get("text", "")) for part in message if isinstance(part, dict))
    if not isinstance(message, (str, dict)) or not message:
        raise OpenAIEnvError("OpenAI returned an empty completion.")
    usage = body.get("usage") or {}
    usage_metadata: dict[str, Any] = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for key in ("cost", "total_cost", "cost_usd"):
        if usage.get(key) is not None:
            usage_metadata[key] = usage[key]
    return {"content": message, "model": body.get("model") or model, "usage": usage_metadata}
