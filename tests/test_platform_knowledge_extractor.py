"""Deterministic knowledge extraction: grounded facts, no model calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.platform_knowledge_extractor import EXTRACTOR_VERSION, extract_knowledge


def _doc(url, text, *, source_type="website", metadata=None, status=200):
    return SimpleNamespace(
        id="doc",
        url=url,
        source_type=source_type,
        content_text=text,
        metadata_json=metadata or {},
        http_status=status,
    )


def _platform():
    return SimpleNamespace(name="Vandefence")


def test_html_signals_produce_grounded_facts():
    text = (
        "رزرو مشاوره حقوقی | خدمات مشاوره خانواده | پشتیبانی آنلاین"
    )
    doc = _doc(
        "https://nobatvakil.com/",
        text,
        metadata={
            "signals": [
                {"text": "رزرو مشاوره حقوقی", "role": "nav"},
                {"text": "پشتیبانی آنلاین", "role": "heading"},
            ],
            "meta_description": "خدمات مشاوره حقوقی",
        },
    )

    knowledge = extract_knowledge(_platform(), [doc])

    assert knowledge.products_and_services[0].evidence == "رزرو مشاوره حقوقی"
    assert knowledge.products_and_services[0].source_url == doc.url
    assert knowledge.capabilities[0].evidence == "پشتیبانی آنلاین"
    assert all(0 < fact.confidence <= 1 for fact in knowledge.capabilities)
    assert all(fact.evidence in text for fact in knowledge.products_and_services)
    assert EXTRACTOR_VERSION.startswith("deterministic")


def test_openapi_operations_become_capabilities_and_workflows():
    spec = {
        "info": {"title": "Vandefence API", "description": "مدیریت پلتفرم و پایش"},
        "tags": [{"name": "پایش", "description": "سرویس پایش پلتفرم"}],
        "paths": {
            "/alerts": {
                "get": {"summary": "دریافت هشدارها"},
                "post": {"summary": "ایجاد هشدار"},
            }
        },
    }
    doc = _doc(
        "https://internal.example.test/openapi.json",
        json.dumps(spec, ensure_ascii=False),
        source_type="openapi",
    )

    knowledge = extract_knowledge(_platform(), [doc])

    assert knowledge.summary == "مدیریت پلتفرم و پایش"
    assert any(fact.name == "پایش" for fact in knowledge.products_and_services)
    assert any(fact.name == "دریافت هشدارها" for fact in knowledge.capabilities)
    assert any(fact.name == "ایجاد هشدار" for fact in knowledge.workflows)
    assert all(fact.source_url == doc.url for fact in knowledge.capabilities)


def test_markdown_headings_and_bullets_are_extracted_verbatim():
    text = "# محصولات\n- رزرو مشاوره حقوقی\n- پشتیبانی آنلاین\n"
    doc = _doc("https://docs.example.test/product.md", text, source_type="document")

    knowledge = extract_knowledge(_platform(), [doc])

    assert any(fact.evidence == "رزرو مشاوره حقوقی" for fact in knowledge.products_and_services)
    assert all(fact.evidence in text for fact in knowledge.capabilities)


def test_unusable_sources_produce_explicit_unknowns_without_invention():
    doc = _doc("https://vandefence.ir/", "Not Found", status=404)

    knowledge = extract_knowledge(_platform(), [doc])

    assert knowledge.summary == ""
    assert not any(
        knowledge.products_and_services
        or knowledge.capabilities
        or knowledge.workflows
        or knowledge.differentiators
    )
    assert knowledge.unknowns


def test_no_documents_yield_only_unknowns():
    knowledge = extract_knowledge(_platform(), [])

    assert knowledge.summary == ""
    assert not knowledge.products_and_services
    assert knowledge.unknowns


def test_plain_text_without_structure_does_not_invent_facts():
    doc = _doc(
        "https://internal.example.test/",
        "صفحه اصلی\nدرباره ما\nتماس با ما\n",
        source_type="internal",
    )

    knowledge = extract_knowledge(_platform(), [doc])

    assert not knowledge.products_and_services
    assert not knowledge.capabilities
    assert knowledge.unknowns
