"""Structured scenario generation on top of the existing text-generation API.

The service deliberately reuses the platform's ``llm_openai`` adapter and its
credential boundary: no second LLM integration is introduced, and the API key
never leaves the credential service. The model is asked for strict JSON; the
response is validated and duration-normalised by ``app.schemas.scenario``.

``ScenarioService`` also accepts an injected ``llm_call`` coroutine, which is
what the tests use. Production code always goes through the provider executor.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.integrations.providers import ProviderExecutor, ProviderRegistry
from app.integrations.providers.base import ProviderError, ProviderExecutionContext
from app.models import PlatformConnection
from app.schemas.scenario import (
    Scenario,
    ScenarioRequest,
    normalize_scenario,
)
from app.security.crypto import MasterKeyBox
from app.services.credential_service import CredentialService

_SCENARIO_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class ScenarioGenerationError(Exception):
    """The text API did not return a usable scenario (message is client-safe)."""

    def __init__(self, message: str, *, code: str = "scenario_generation_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


LlmCall = Callable[..., Awaitable[Any]]


def build_scenario_prompt(request: ScenarioRequest) -> tuple[str, str]:
    """Return ``(system, user)`` prompts for scenario generation."""
    scene_count = request.resolved_scene_count
    tone = request.brand_tone or "حرفه‌ای، روشن و قابل اعتماد؛ بدون اغراق و بدون ادعای اثبات‌نشده."
    brand = request.brand_name or "برند"
    cta_hint = request.cta_text or "یک دعوت به اقدام کوتاه و روشن"
    hashtags_hint = "، ".join(request.hashtags) if request.hashtags else "سه تا پنج هشتگ مرتبط"

    system = (
        "تو یک نویسنده و کارگردان محتوای ویدیویی کوتاه فارسی هستی. "
        "خروجی را فقط به صورت یک JSON معتبر و بدون هیچ متن اضافه برگردان. "
        "روایت باید فارسی طبیعی، روان و گفتاری باشد؛ نه ترجمه ماشینی و نه بازاریابی ترس‌محور. "
        "از تکرار جمله‌ها، اغراق، وعده تضمینی و ادعای اثبات‌نشده درباره محصول، "
        "عملکرد، مشتری یا امنیت پرهیز کن. اگر عدد یا قابلیت مشخصی را نمی‌دانی، آن را ادعا نکن. "
        "توضیح تصویری هر صحنه را به انگلیسی و دقیق بنویس تا مدل تولید ویدیو آن را بفهمد، "
        "اما روایت، متن روی تصویر و زیرنویس باید فارسی باشند."
    )
    user = (
        f"برای برند «{brand}» یک سناریوی ویدیوی عمودی {request.target_duration_seconds} ثانیه‌ای بساز. "
        f"لحن برند: {tone}\n"
        f"موضوع: {request.topic}\n"
        f"تعداد صحنه: دقیقاً {scene_count} صحنه (حدود ۵ تا ۶ صحنه برای هر ۳۰ ثانیه).\n"
        f"دعوت به اقدام پیشنهادی: {cta_hint}\n"
        f"هشتگ‌های پیشنهادی: {hashtags_hint}\n\n"
        "ساختار JSON الزامی:\n"
        "{\n"
        '  "title": "عنوان کوتاه فارسی",\n'
        '  "opening_hook": "جمله قلاب اول ویدیو به فارسی",\n'
        '  "visual_style": "توضیح کوتاه سبک بصری به انگلیسی",\n'
        '  "scenes": [\n'
        "    {\n"
        '      "index": 0,\n'
        '      "narration": "روایت فارسی این صحنه",\n'
        '      "visual_prompt": "Detailed English visual prompt for a vertical video scene",\n'
        '      "duration_seconds": 5,\n'
        '      "on_screen_text": "متن کوتاه فارسی روی تصویر",\n'
        '      "subtitle_text": "زیرنویس فارسی این صحنه"\n'
        "    }\n"
        "  ],\n"
        '  "cta": "دعوت به اقدام کوتاه فارسی",\n'
        '  "caption": "کپشن فارسی برای انتشار",\n'
        '  "hashtags": ["#هشتگ"]\n'
        "}\n\n"
        "همه روایت‌ها با هم باید تقریباً کل مدت درخواستی را پر کنند. "
        "هیچ صحنه‌ای بدون narration و visual_prompt نباشد."
    )
    return system, user


def parse_scenario_content(content: Any, request: ScenarioRequest) -> Scenario:
    """Parse a model response into a validated ``Scenario``."""
    if isinstance(content, dict):
        payload: Any = content
    elif isinstance(content, str):
        cleaned = _SCENARIO_FENCE.sub("", content).strip()
        if not cleaned:
            raise ScenarioGenerationError("The text API returned an empty scenario.")
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ScenarioGenerationError(
                "The text API returned malformed JSON for the scenario.",
                code="scenario_invalid_json",
            ) from exc
    else:
        raise ScenarioGenerationError("The text API returned an unsupported scenario payload.")

    if isinstance(payload, list):
        payload = {"scenes": payload}

    try:
        return normalize_scenario(payload, request)
    except ScenarioGenerationError:
        raise
    except Exception as exc:  # pydantic ValidationError and structural errors
        raise ScenarioGenerationError(
            f"The generated scenario is not usable: {exc}",
            code="scenario_invalid",
        ) from exc


class ScenarioService:
    """Generate a validated scenario through the configured text-generation API."""

    def __init__(
        self,
        session: AsyncSession,
        registry: ProviderRegistry,
        executor: ProviderExecutor,
        master_key_box: MasterKeyBox,
        *,
        llm_call: LlmCall | None = None,
    ) -> None:
        self.session = session
        self.registry = registry
        self.executor = executor
        self._keys = master_key_box
        self._llm_call = llm_call

    async def generate(
        self,
        *,
        company_id: uuid.UUID,
        connection_id: uuid.UUID,
        request: ScenarioRequest,
        timeout_seconds: float = 120.0,
        operation: str = "execute",
    ) -> tuple[Scenario, dict[str, Any]]:
        """Return the validated scenario plus sanitized LLM metadata."""
        system, user = build_scenario_prompt(request)
        metadata = await self._complete(
            company_id=company_id,
            connection_id=connection_id,
            system=system,
            user=user,
            timeout_seconds=timeout_seconds,
            operation=operation,
        )
        scenario = parse_scenario_content(metadata.get("content"), request)
        return scenario, metadata

    async def _complete(
        self,
        *,
        company_id: uuid.UUID,
        connection_id: uuid.UUID,
        system: str,
        user: str,
        timeout_seconds: float,
        operation: str,
    ) -> dict[str, Any]:
        if self._llm_call is not None:
            raw = await self._llm_call(system=system, user=user)
            if isinstance(raw, dict):
                return raw
            return {"content": raw}

        adapter = self.registry.get("llm_openai")
        connection = await self.session.get(PlatformConnection, connection_id)
        if connection is None or connection.company_id != company_id:
            raise NotFoundError(
                "LLM connection not found.",
                details=[{"field": "connection_id", "value": str(connection_id)}],
            )

        credential = None
        credential_type = None
        secret_reference = None
        if connection.credential_reference_id is not None:
            resolved = await CredentialService(self.session, self._keys).resolve_credential(
                connection.credential_reference_id, company_id=company_id
            )
            credential = resolved.payload
            credential_type = resolved.credential_type
            secret_reference = resolved.secret_reference

        context = ProviderExecutionContext(
            provider_id="llm_openai",
            connection_id=connection.id,
            company_id=company_id,
            platform_id=connection.platform_id,
            configuration={
                **connection.configuration,
                "params": {"system": system, "user": user},
                "response_format": "json_object",
                "temperature": 0.4,
                "max_tokens": 2000,
            },
            credential=credential,
            credential_type=credential_type,
            secret_reference=secret_reference,
            timeout_seconds=timeout_seconds,
            operation=operation,
        )
        outcome = await self.executor.run(adapter, context, adapter.execute, idempotent=True)
        if not outcome.success:
            error = outcome.error or ProviderError("The text API request failed.", code="llm_failure")
            raise ScenarioGenerationError(error.message, code=error.code or "llm_failure")
        return outcome.result.metadata or {}
