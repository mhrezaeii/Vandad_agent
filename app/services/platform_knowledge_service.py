"""Deterministic source-grounded knowledge snapshots (no model calls)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Platform, PlatformKnowledgeSnapshot, PlatformSourceDocument
from app.schemas.platform_knowledge import PlatformKnowledgePayload
from app.services.platform_knowledge_extractor import EXTRACTOR_VERSION, extract_knowledge
from app.services.platform_knowledge_text import normalise_evidence_text  # noqa: F401 - re-exported for callers

__all__ = [
    "EXTRACTOR_VERSION",
    "PlatformKnowledgeExtractionError",
    "PlatformKnowledgeService",
    "normalise_evidence_text",
    "validate_knowledge_evidence",
]


class PlatformKnowledgeExtractionError(Exception):
    def __init__(
        self, message: str, *, code: str = "knowledge_extraction_failed", retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


def _fact_to_text(value: Any) -> str | None:
    """Flatten a string or a fact object into plain text."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "description", "summary", "text"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _normalise_knowledge_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce free-text shape deviations before strict validation.

    Retained for compatibility with stored/external payloads that were shaped
    by earlier extraction integrations. The deterministic extractor builds a
    valid ``PlatformKnowledgePayload`` directly and does not need this step.
    """
    normalised = dict(raw)
    summary = normalised.get("summary")
    if isinstance(summary, list):
        parts = [text for text in (_fact_to_text(item) for item in summary) if text]
        normalised["summary"] = "\n".join(parts)[:5000]
    elif not isinstance(summary, str):
        normalised["summary"] = ""
    for key, limit in (("unknowns", 50), ("content_opportunities", 30)):
        items = normalised.get(key)
        if isinstance(items, list):
            normalised[key] = [
                text for text in (_fact_to_text(item) for item in items) if text
            ][:limit]
        elif not isinstance(items, list):
            normalised[key] = []
    return normalised


def _parse_payload(raw: Any, allowed_urls: set[str]) -> PlatformKnowledgePayload:
    """Validate a serialised knowledge payload against the known source URLs."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError as exc:
            raise PlatformKnowledgeExtractionError(
                "The supplied payload was not valid JSON.", code="knowledge_invalid_json"
            ) from exc
    if not isinstance(raw, dict):
        raise PlatformKnowledgeExtractionError("The supplied payload had an unsupported shape.")
    try:
        payload = PlatformKnowledgePayload.model_validate(_normalise_knowledge_shape(raw))
    except Exception as exc:
        raise PlatformKnowledgeExtractionError(
            "The knowledge payload did not match the required schema.", code="knowledge_invalid_schema"
        ) from exc
    allowed = {url.rstrip("/") for url in allowed_urls}
    for group in (
        payload.products_and_services, payload.capabilities, payload.workflows,
        payload.target_audiences, payload.differentiators,
    ):
        for fact in group:
            if fact.source_url.rstrip("/") not in allowed:
                raise PlatformKnowledgeExtractionError(
                    "A knowledge fact referenced a URL outside the supplied source set.",
                    code="knowledge_invalid_source",
                )
    return payload


_EVIDENCE_SEGMENT_SPLIT = re.compile(r"[.!?؟|\n]+")


def _best_evidence_excerpt(evidence: str, raw_source: str) -> str | None:
    """Find a verbatim source segment that supports a compressed excerpt."""
    tokens = {token for token in normalise_evidence_text(evidence).split() if len(token) >= 2}
    if not tokens:
        return None
    best: tuple[float, int, str] | None = None
    for raw_segment in _EVIDENCE_SEGMENT_SPLIT.split(raw_source):
        segment = " ".join(raw_segment.split())
        if not segment:
            continue
        normalised = normalise_evidence_text(segment)
        hits = sum(1 for token in tokens if token in normalised)
        coverage = hits / len(tokens)
        if hits < min(2, len(tokens)) or coverage < 0.6:
            continue
        candidate = (coverage, -len(segment), segment)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return None if best is None else best[2][:1000]


def validate_knowledge_evidence(
    payload: PlatformKnowledgePayload, documents: list[PlatformSourceDocument]
) -> None:
    """Ground every fact's evidence in its cited source text, or reject the fact."""
    source_text = {
        (document.url or "").rstrip("/"): document.content_text or ""
        for document in documents
    }
    groups = (
        payload.products_and_services, payload.capabilities, payload.workflows,
        payload.target_audiences, payload.differentiators,
    )
    for group in groups:
        for fact in group:
            raw_source = (
                source_text.get(fact.source_url.rstrip("/"))
                or source_text.get(fact.source_url + "/")
            )
            if raw_source is None:
                raise PlatformKnowledgeExtractionError(
                    f"A knowledge fact cited an unknown source URL (fact: {fact.name[:80]}).",
                    code="knowledge_invalid_source",
                )
            if normalise_evidence_text(fact.evidence) in normalise_evidence_text(raw_source):
                continue
            grounded = _best_evidence_excerpt(fact.evidence, raw_source)
            if grounded is None:
                raise PlatformKnowledgeExtractionError(
                    f"A knowledge fact contained evidence not found in its cited source "
                    f"(fact: {fact.name[:80]}).",
                    code="knowledge_evidence_mismatch",
                )
            fact.evidence = grounded


class PlatformKnowledgeService:
    """Persist facts extracted deterministically from the latest source documents."""

    def __init__(
        self,
        session: AsyncSession,
        registry: Any = None,
        executor: Any = None,
        master_key_box: Any = None,
    ) -> None:
        # Provider dependencies are accepted for call-site compatibility but
        # knowledge extraction never uses them.
        self.session = session

    async def extract(
        self, *, platform: Platform, connection_id: uuid.UUID | None = None,
        max_documents: int = 20, task_id: uuid.UUID | None = None,
    ) -> PlatformKnowledgeSnapshot:
        """Build one snapshot from the newest grounded source documents."""
        documents = list((await self.session.scalars(
            select(PlatformSourceDocument).where(
                PlatformSourceDocument.company_id == platform.company_id,
                PlatformSourceDocument.platform_id == platform.id,
            ).order_by(PlatformSourceDocument.last_seen_at.desc()).limit(max_documents)
        )).all())
        # A platform with no usable sources still gets an honest snapshot that
        # records the gap as unknowns. This keeps the automatic sweep resilient
        # (e.g. a public site returning 404) without inventing any fact.
        knowledge = extract_knowledge(platform, documents)
        validate_knowledge_evidence(knowledge, documents)
        fingerprint = hashlib.sha256(
            "|".join(sorted(f"{doc.id}:{doc.content_hash}" for doc in documents)).encode()
        ).hexdigest()
        snapshot = PlatformKnowledgeSnapshot(
            company_id=platform.company_id,
            platform_id=platform.id,
            source_document_ids=[str(document.id) for document in documents],
            source_fingerprint=fingerprint,
            knowledge_json=knowledge.model_dump(mode="json"),
            model=EXTRACTOR_VERSION,
            extracted_at=datetime.now(UTC),
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot
