from types import SimpleNamespace

import pytest

from app.services.platform_knowledge_service import (
    PlatformKnowledgeExtractionError,
    _parse_payload,
    validate_knowledge_evidence,
)


def _payload(source_url: str = "https://example.com/features") -> dict:
    return {
        "summary": "سامانه برای مدیریت نوبت است.",
        "products_and_services": [
            {
                "name": "مدیریت نوبت",
                "description": "امکان مدیریت نوبت‌های مشتریان",
                "evidence": "مدیریت نوبت مشتریان",
                "source_url": source_url,
                "confidence": 0.91,
            }
        ],
        "capabilities": [],
        "workflows": [],
        "target_audiences": [],
        "differentiators": [],
        "content_opportunities": ["آموزش مدیریت نوبت"],
        "unknowns": [],
    }


def test_parse_payload_accepts_only_supplied_source_urls():
    result = _parse_payload(_payload(), {"https://example.com/features"})
    assert result.products_and_services[0].name == "مدیریت نوبت"


def test_parse_payload_rejects_unknown_source_url():
    with pytest.raises(PlatformKnowledgeExtractionError) as exc:
        _parse_payload(_payload("https://attacker.example/fake"), {"https://example.com/features"})
    assert exc.value.code == "knowledge_invalid_source"


def test_parse_payload_rejects_invalid_schema():
    invalid = _payload()
    invalid["products_and_services"][0]["confidence"] = 1.5
    with pytest.raises(PlatformKnowledgeExtractionError) as exc:
        _parse_payload(invalid, {"https://example.com/features"})
    assert exc.value.code == "knowledge_invalid_schema"


def test_evidence_must_exist_in_the_exact_cited_source():
    payload = _parse_payload(_payload(), {"https://example.com/features"})
    documents = [
        SimpleNamespace(
            url="https://example.com/features",
            content_text="این صفحه درباره مدیریت نوبت مشتریان و گزارش‌گیری است.",
        )
    ]
    validate_knowledge_evidence(payload, documents)


def test_evidence_mismatch_is_rejected():
    payload = _parse_payload(_payload(), {"https://example.com/features"})
    documents = [
        SimpleNamespace(
            url="https://example.com/features",
            content_text="این صفحه فقط درباره تماس با ما است.",
        )
    ]
    with pytest.raises(PlatformKnowledgeExtractionError) as exc:
        validate_knowledge_evidence(payload, documents)
    assert exc.value.code == "knowledge_evidence_mismatch"
