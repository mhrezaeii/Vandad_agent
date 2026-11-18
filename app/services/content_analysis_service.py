"""Deterministic content analysis over monitored brand knowledge.

The monitoring agent produces source-grounded knowledge snapshots. This service
turns those snapshots into ranked content candidates: it normalises text,
removes duplicates, scores brand fit, keeps the original source references and
selects one topic. It never calls an LLM — the only model call in the content
pipeline remains scenario generation, so analysis is cheap and reproducible.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models import (
    BrandPlatformLink,
    ContentBrand,
    PlatformKnowledgeSnapshot,
)

# Fact groups produced by ``PlatformKnowledgeService``. Order matters: earlier
# groups make stronger advertising topics than generic audience notes.
_FACT_GROUPS: tuple[tuple[str, float], ...] = (
    ("products_and_services", 0.10),
    ("capabilities", 0.08),
    ("differentiators", 0.06),
    ("workflows", 0.05),
    ("target_audiences", 0.03),
)

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_NON_WORD_RE = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)
_TRANSLATION = str.maketrans({"ي": "ی", "ك": "ک"})

_MAX_TOPIC_CHARS = 240
_MAX_DESCRIPTION_CHARS = 400


def normalise_text(value: str) -> str:
    """Unicode-normalise Persian text so duplicate detection is stable."""
    text = unicodedata.normalize("NFKC", value or "").translate(_TRANSLATION)
    text = _DIACRITICS_RE.sub("", text).casefold()
    return _NON_WORD_RE.sub(" ", text).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in normalise_text(value).split() if len(token) > 1}


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class ContentAnalysisService:
    """Rank and select content topics from verified knowledge snapshots."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def select(
        self,
        *,
        company_id: uuid.UUID,
        brand_id: uuid.UUID,
        topic: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        brand = await self.session.get(ContentBrand, brand_id)
        if brand is None or brand.company_id != company_id or brand.status != "active":
            raise NotFoundError("Active content brand not found for this company.")

        platform_ids = list(
            (
                await self.session.scalars(
                    select(BrandPlatformLink.platform_id).where(
                        BrandPlatformLink.company_id == company_id,
                        BrandPlatformLink.brand_id == brand.id,
                    )
                )
            ).all()
        )
        if not platform_ids:
            raise ConflictError(
                "Link at least one monitored platform to this brand before analysing topics."
            )

        snapshots = list(
            (
                await self.session.scalars(
                    select(PlatformKnowledgeSnapshot)
                    .where(
                        PlatformKnowledgeSnapshot.company_id == company_id,
                        PlatformKnowledgeSnapshot.platform_id.in_(platform_ids),
                    )
                    .order_by(PlatformKnowledgeSnapshot.extracted_at.desc())
                )
            ).all()
        )
        latest_by_platform: dict[str, PlatformKnowledgeSnapshot] = {}
        for snapshot in snapshots:
            latest_by_platform.setdefault(str(snapshot.platform_id), snapshot)
        if not latest_by_platform:
            raise ConflictError(
                "No verified platform knowledge snapshot exists for the linked brand platforms."
            )

        brand_tokens = _tokens(
            " ".join(
                value
                for value in (
                    brand.display_name,
                    brand.name,
                    brand.description,
                    brand.industry,
                    brand.tone,
                    brand.domain,
                )
                if isinstance(value, str) and value.strip()
            )
        )
        candidates, duplicate_count = self._collect_candidates(
            latest_by_platform.values(), brand_tokens
        )
        candidates.sort(key=lambda item: (-item["brand_fit"], item["topic"]))
        ranked = candidates[:limit]

        requested_topic = _clean(topic, _MAX_TOPIC_CHARS)
        if requested_topic and len(requested_topic) >= 3:
            selected_topic = requested_topic
            supporting = self._supporting_facts(ranked, requested_topic, brand_tokens)
            brand_fit = self._topic_fit(requested_topic, supporting, brand_tokens)
            selection_reason = (
                "موضوع درخواستی کاربر با شواهد پشتیبان برند تطبیق داده شد."
                if supporting
                else "موضوع درخواستی کاربر پذیرفته شد؛ شاهد مستقیمی در دانش پایش‌شده یافت نشد."
            )
        elif ranked:
            selected = ranked[0]
            selected_topic = selected["topic"]
            supporting = self._supporting_facts(ranked, selected_topic, brand_tokens)
            brand_fit = selected["brand_fit"]
            selection_reason = "بالاترین امتیاز تطابق برند در میان نامزدهای بدون تکرار."
        else:
            raise ConflictError(
                "Monitored knowledge did not produce any usable content candidate."
            )

        sources: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        for item in [*supporting, *ranked]:
            source_url = item.get("source_url")
            if not source_url or source_url in seen_sources:
                continue
            seen_sources.add(source_url)
            sources.append(
                {
                    "platform_id": item["platform_id"],
                    "source_url": source_url,
                    "evidence": item.get("evidence") or "",
                }
            )
            if len(sources) >= 5:
                break

        return {
            "brand_id": str(brand.id),
            "brand_name": brand.display_name or brand.name,
            "selected_topic": selected_topic,
            "selection_reason": selection_reason,
            "brand_fit": brand_fit,
            "candidate_count": len(candidates),
            "duplicate_count": duplicate_count,
            "source_count": len(seen_sources),
            "platform_count": len(latest_by_platform),
            "snapshot_ids": [str(snapshot.id) for snapshot in latest_by_platform.values()],
            "sources": sources,
            "supporting_points": [
                {
                    "topic": item["topic"],
                    "description": item["description"],
                    "source_url": item.get("source_url"),
                    "brand_fit": item["brand_fit"],
                }
                for item in supporting
            ],
            "candidates": ranked,
        }

    # ------------------------------------------------------------------ internals
    def _collect_candidates(
        self,
        snapshots: Any,
        brand_tokens: set[str],
    ) -> tuple[list[dict[str, Any]], int]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates = 0

        for snapshot in snapshots:
            knowledge = snapshot.knowledge_json or {}
            platform_id = str(snapshot.platform_id)
            summary = _clean(knowledge.get("summary"), 500)
            for group, weight in _FACT_GROUPS:
                for fact in knowledge.get(group) or []:
                    if not isinstance(fact, dict):
                        continue
                    name = _clean(fact.get("name"), _MAX_TOPIC_CHARS)
                    description = _clean(fact.get("description"), _MAX_DESCRIPTION_CHARS)
                    topic = name if len(name) >= 3 else description[:_MAX_TOPIC_CHARS]
                    if len(topic) < 3:
                        continue
                    key = normalise_text(topic)
                    if not key:
                        continue
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    candidates.append(
                        self._candidate(
                            topic=topic,
                            description=description,
                            group=group,
                            weight=weight,
                            platform_id=platform_id,
                            fact=fact,
                            brand_tokens=brand_tokens,
                        )
                    )

            if not any(
                item["platform_id"] == platform_id for item in candidates
            ) and len(summary) >= 3:
                # Fallback for snapshots that only contain a narrative summary.
                key = normalise_text(summary)
                if key and key not in seen:
                    seen.add(key)
                    candidates.append(
                        self._candidate(
                            topic=summary[:_MAX_TOPIC_CHARS],
                            description=summary,
                            group="summary",
                            weight=0.02,
                            platform_id=platform_id,
                            fact={},
                            brand_tokens=brand_tokens,
                        )
                    )

        return candidates, duplicates

    @staticmethod
    def _candidate(
        *,
        topic: str,
        description: str,
        group: str,
        weight: float,
        platform_id: str,
        fact: dict[str, Any],
        brand_tokens: set[str],
    ) -> dict[str, Any]:
        topic_tokens = _tokens(f"{topic} {description}")
        overlap = (
            len(topic_tokens & brand_tokens) / len(topic_tokens)
            if topic_tokens and brand_tokens
            else 0.0
        )
        try:
            confidence = float(fact.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        source_url = _clean(fact.get("source_url"), 2048) or None
        evidence = _clean(fact.get("evidence"), 400)
        score = (
            0.30
            + weight
            + (0.15 if source_url else 0.0)
            + (0.10 if evidence else 0.0)
            + (0.20 * overlap)
            + (0.15 * confidence)
        )
        return {
            "topic": topic,
            "description": description,
            "group": group,
            "platform_id": platform_id,
            "source_url": source_url,
            "evidence": evidence,
            "confidence": round(confidence, 3),
            "brand_fit": _clamp(score),
        }

    @staticmethod
    def _supporting_facts(
        candidates: list[dict[str, Any]],
        topic: str,
        brand_tokens: set[str],
    ) -> list[dict[str, Any]]:
        topic_tokens = _tokens(topic)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in candidates:
            item_tokens = _tokens(f"{item['topic']} {item['description']}")
            shared = len(topic_tokens & item_tokens)
            if shared == 0 and brand_tokens:
                continue
            score = shared / max(1, len(topic_tokens)) + item["brand_fit"]
            scored.append((score, item))
        scored.sort(key=lambda pair: -pair[0])
        supporting = [item for _score, item in scored[:5]]
        if supporting:
            return supporting
        return candidates[:3]

    @staticmethod
    def _topic_fit(
        topic: str,
        supporting: list[dict[str, Any]],
        brand_tokens: set[str],
    ) -> float:
        topic_tokens = _tokens(topic)
        overlap = (
            len(topic_tokens & brand_tokens) / len(topic_tokens)
            if topic_tokens and brand_tokens
            else 0.0
        )
        evidence_bonus = (
            sum(1 for item in supporting if item.get("source_url"))
            / max(1, len(supporting))
            * 0.2
        )
        return _clamp(0.5 + 0.3 * overlap + evidence_bonus)
