"""Deterministic, LLM-free extraction of source-grounded platform knowledge.

This module is the *only* producer of ``PlatformKnowledgePayload`` in the
monitoring pipeline. It never calls a model: every fact is derived from a
structural signal in a persisted source document (heading, navigation item,
list, form label, FAQ pair, OpenAPI operation, or markdown heading), and every
evidence string is copied verbatim from that document's text. Roles and
source multiplicity provide a deterministic confidence score. When a source
does not support a fact, the group stays empty and an explicit ``unknown`` is
emitted instead of guessing.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from app.models import Platform, PlatformSourceDocument
from app.schemas.platform_knowledge import PlatformKnowledgeFact, PlatformKnowledgePayload
from app.services.platform_knowledge_text import normalise_evidence_text

EXTRACTOR_VERSION = "deterministic-v1"

_FACT_LIMITS = {
    "products_and_services": 12,
    "capabilities": 20,
    "workflows": 12,
    "target_audiences": 8,
    "differentiators": 8,
}

_GROUP_LABELS = {
    "products_and_services": "محصولات و خدمات",
    "capabilities": "قابلیتها",
    "workflows": "فرآیندها و گردشکارها",
    "target_audiences": "مخاطبان هدف",
    "differentiators": "مزایای رقابتی",
}

_GROUP_PATTERNS: dict[str, tuple[str, ...]] = {
    "products_and_services": (
        "محصول", "خدمات", "سرویس", "پکیج", "بسته", "تعرفه", "قیمت",
        "اشتراک", "product", "service", "pricing", "plan",
    ),
    "workflows": (
        "رزرو", "ثبت", "شروع", "مراحل", "نحوه", "چگونه", "گام", "درخواست",
        "تماس", "خرید", "پرداخت", "how to", "step", "workflow", "booking",
    ),
    "target_audiences": (
        "مناسب", "مخاطب", "کاربران", "مشتریان", "برای ", "کسبوکار",
        "وکلا", "مشاور", "تیمها", "audience", "who", "for ",
    ),
    "differentiators": (
        "چرا ", "مزایا", "تفاوت", "برتر", "منحصر", "تضمین", "امتیاز",
        "advantage", "why ", "unique",
    ),
    "capabilities": (
        "قابلیت", "ویژگی", "امکان", "پشتیبانی", "اتصال", "پایش", "گزارش",
        "feature", "capability", "support", "integrat", "dashboard",
    ),
}

_ROLE_CONFIDENCE = {
    "openapi": 0.90,
    "nav": 0.84,
    "faq": 0.82,
    "meta": 0.80,
    "heading": 0.78,
    "form": 0.76,
    "workflow": 0.76,
    "list": 0.70,
    "text": 0.52,
}

_SIGNAL_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "label", "button", "a"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}
_BULLET_RE = re.compile(r"^\s*(?:[-*•–—]|\d+[.)])\s*")
_MAX_EVIDENCE = 1000


def _classify(text: str, role: str) -> str | None:
    """Map a structural signal to a fact group using explicit keyword rules."""
    if role.startswith("context_"):
        return role.removeprefix("context_")
    if role in {"nav", "openapi_tag"}:
        return "products_and_services"
    if role == "faq":
        return "differentiators"
    if role in {"form", "workflow"}:
        return "workflows"
    normalised = normalise_evidence_text(text)
    for group, patterns in _GROUP_PATTERNS.items():
        if any(pattern in normalised for pattern in patterns):
            return group
    if role in {"heading", "meta", "openapi"}:
        return "capabilities"
    return None


def _bounded(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _grounded(doc: PlatformSourceDocument, value: str) -> bool:
    return normalise_evidence_text(value) in normalise_evidence_text(doc.content_text or "")


def _signals_from_html(doc: PlatformSourceDocument) -> list[dict[str, str]]:
    """Read structural signals persisted by the crawler in ``metadata_json``."""
    metadata = doc.metadata_json or {}
    signals = metadata.get("signals") or []
    result: list[dict[str, str]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        text = _bounded(str(signal.get("text") or ""), _MAX_EVIDENCE)
        role = str(signal.get("role") or "")
        if len(text) >= 3 and role and _grounded(doc, text):
            result.append({"text": text, "role": role})
    return result


def _signals_from_openapi(doc: PlatformSourceDocument) -> list[dict[str, str]]:
    """Turn an OpenAPI document into grounded capability/workflow signals."""
    try:
        spec = json.loads(doc.content_text or "")
    except (TypeError, ValueError):
        return []
    if not isinstance(spec, dict):
        return []
    result: list[dict[str, str]] = []
    for tag in spec.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        description = _bounded(str(tag.get("description") or ""), _MAX_EVIDENCE)
        name = _bounded(str(tag.get("name") or ""), 200)
        if name and _grounded(doc, name):
            result.append({"text": name, "role": "openapi_tag"})
        if description and _grounded(doc, description):
            result.append({"text": description, "role": "openapi"})
    for _path, operations in (spec.get("paths") or {}).items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if not isinstance(operation, dict) or method.lower() not in {
                "get", "post", "put", "patch", "delete"
            }:
                continue
            summary = _bounded(str(operation.get("summary") or ""), _MAX_EVIDENCE)
            description = _bounded(str(operation.get("description") or ""), _MAX_EVIDENCE)
            role = "workflow" if method.lower() in {"post", "put", "patch", "delete"} else "openapi"
            for value in (summary, description):
                if value and _grounded(doc, value):
                    result.append({"text": value, "role": role})
    return result


def _signals_from_markdown(doc: PlatformSourceDocument) -> list[dict[str, str]]:
    """Read markdown headings and bullets as deterministic structural signals.

    Bullets inherit the fact group of the nearest heading, so a «محصولات»
    section does not get misclassified as a workflow because of a keyword.
    """
    result: list[dict[str, str]] = []
    context_group: str | None = None
    for line in (doc.content_text or "").splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        bullet = _BULLET_RE.match(line)
        if heading:
            text = _bounded(heading.group(1), _MAX_EVIDENCE)
            role = "heading"
        elif bullet:
            text = _bounded(line[bullet.end():], _MAX_EVIDENCE)
            role = "list"
            if context_group:
                role = f"context_{context_group}"
        else:
            continue
        if len(text) < 3 or not _grounded(doc, text):
            continue
        result.append({"text": text, "role": role})
        if heading:
            context_group = _classify(text, role)
    return result


def _signals_from_plain_text(doc: PlatformSourceDocument) -> list[dict[str, str]]:
    """Conservative fallback for internal/plain pages without markup signals."""
    result: list[dict[str, str]] = []
    for line in (doc.content_text or "").splitlines():
        text = _bounded(line, _MAX_EVIDENCE)
        if len(text) < 4 or len(text) > 180:
            continue
        if text[-1:] in ".!?؟:":
            continue
        if any(char in text for char in "{}[]<>|"):
            continue
        if not _grounded(doc, text):
            continue
        group = _classify(text, "text")
        if group is not None:
            result.append({"text": text, "role": "text"})
    return result[:40]


def _collect_signals(doc: PlatformSourceDocument) -> list[dict[str, str]]:
    source_type = (doc.source_type or "website").lower()
    if source_type in {"openapi", "swagger"}:
        return _signals_from_openapi(doc)
    if source_type in {"document", "markdown", "md"}:
        return _signals_from_markdown(doc)
    signals = _signals_from_html(doc)
    if signals:
        return signals
    return _signals_from_plain_text(doc)


def _summary(documents: list[PlatformSourceDocument], signals: list[dict[str, str]]) -> str:
    for doc in documents:
        metadata = doc.metadata_json or {}
        if (doc.source_type or "").lower() in {"openapi", "swagger"}:
            try:
                spec = json.loads(doc.content_text or "")
                description = _bounded(str((spec.get("info") or {}).get("description") or ""), 1000)
                if description and _grounded(doc, description):
                    return description
            except (TypeError, ValueError, AttributeError):
                pass
        meta = _bounded(str(metadata.get("meta_description") or ""), 1000)
        if meta and _grounded(doc, meta):
            return meta
    for item in signals:
        if item["role"] in {"heading", "openapi", "openapi_tag"}:
            return _bounded(item["text"], 1000)
    for doc in documents:
        text = _bounded(doc.content_text or "", 1000)
        if text:
            return text
    return ""


def extract_knowledge(
    platform: Platform, documents: list[PlatformSourceDocument]
) -> PlatformKnowledgePayload:
    """Extract grounded facts from persisted documents without any model call."""
    usable = [
        doc for doc in documents
        if doc.http_status is None or int(doc.http_status) < 400
    ]
    if not usable:
        return PlatformKnowledgePayload(
            summary="",
            unknowns=[
                f"هیچ سند معتبری از منابع {platform.name} در دسترس نیست؛ "
                "پایش منبع با خطا مواجه شده یا هنوز اجرا نشده است."
            ],
        )

    all_signals: list[tuple[PlatformSourceDocument, dict[str, str]]] = []
    for doc in usable:
        for signal in _collect_signals(doc):
            all_signals.append((doc, signal))

    name_counts: Counter[str] = Counter(
        normalise_evidence_text(signal["text"]) for _doc, signal in all_signals
    )
    seen: dict[str, set[str]] = {group: set() for group in _FACT_LIMITS}
    facts: dict[str, list[PlatformKnowledgeFact]] = {group: [] for group in _FACT_LIMITS}

    for doc, signal in all_signals:
        text = signal["text"]
        group = _classify(text, signal["role"])
        if group is None:
            continue
        key = normalise_evidence_text(text)
        if key in seen[group]:
            continue
        if len(facts[group]) >= _FACT_LIMITS[group]:
            continue
        seen[group].add(key)
        base = _ROLE_CONFIDENCE.get(signal["role"], 0.55)
        multiplicity = min(0.08, 0.02 * max(0, name_counts[key] - 1))
        confidence = round(min(0.95, base + multiplicity), 2)
        facts[group].append(
            PlatformKnowledgeFact(
                name=_bounded(text, 200),
                description=_bounded(text, 500),
                evidence=text,
                source_url=doc.url,
                confidence=confidence,
            )
        )

    unknowns: list[str] = []
    for group, label in _GROUP_LABELS.items():
        if not facts[group]:
            unknowns.append(
                f"هیچ شاهد ساختاریافته و معتبری برای «{label}» در اسناد منبع یافت نشد."
            )

    return PlatformKnowledgePayload(
        summary=_summary(usable, [signal for _doc, signal in all_signals]),
        products_and_services=facts["products_and_services"],
        capabilities=facts["capabilities"],
        workflows=facts["workflows"],
        target_audiences=facts["target_audiences"],
        differentiators=facts["differentiators"],
        content_opportunities=[],
        unknowns=unknowns,
    )
