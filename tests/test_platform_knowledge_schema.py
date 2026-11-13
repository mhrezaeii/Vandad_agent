import pytest




def test_parse_payload_flattens_fact_shaped_summary_and_unknowns():
    """Models may return summary/unknowns as fact objects; extraction must survive.

    Regression: gpt-6-luna returned summary as a list of fact dicts and
    unknowns as dicts, which failed strict validation and 422'd the endpoint.
    """
    from app.services.platform_knowledge_service import _parse_payload

    url = "https://vandiyar.ir/"
    raw = {
        "summary": [
            {"name": "وندیار مدیریت پروژه", "description": "پلتفرم مدیریت پروژه و تیم", "confidence": 0.99},
        ],
        "products_and_services": [
            {
                "name": "مدیریت پروژه",
                "description": "مدیریت پروژه‌ها، گفت‌وگوها، وظایف و فایل‌ها",
                "evidence": "پروژه ها، گفت و گو ها، وظایف و فایل های تیم خود",
                "source_url": url,
                "confidence": 0.95,
            }
        ],
        "capabilities": [],
        "workflows": [],
        "target_audiences": [],
        "differentiators": [],
        "content_opportunities": [{"name": "معرفی قابلیت‌ها"}],
        "unknowns": [
            {"name": "جزئیات قیمت‌گذاری", "confidence": 0.9},
            "قیمت‌ها در دسترس نیست",
        ],
    }
    payload = _parse_payload(raw, {url})
    assert "وندیار مدیریت پروژه" in payload.summary
    assert payload.unknowns == ["جزئیات قیمت‌گذاری", "قیمت‌ها در دسترس نیست"]
    assert payload.content_opportunities == ["معرفی قابلیت‌ها"]
    assert payload.products_and_services[0].name == "مدیریت پروژه"


def test_evidence_validation_tolerates_unicode_normalisation():
    """ZWNJ/digit/kaf-yeh drift between crawl and model must not fail evidence."""
    from types import SimpleNamespace

    from app.schemas.platform_knowledge import PlatformKnowledgeFact, PlatformKnowledgePayload
    from app.services.platform_knowledge_service import PlatformKnowledgeExtractionError, validate_knowledge_evidence

    # Crawl text with ZWNJ: "می‌کند"; model writes "میکند" without it.
    doc = SimpleNamespace(url="https://vandiyar.ir/", content_text="تیم شما هر روز کار می‌کند و پروژه‌ها پیش می‌روند.")
    payload = PlatformKnowledgePayload(
        summary="",
        products_and_services=[PlatformKnowledgeFact(
            name="مدیریت پروژه",
            description="مدیریت کار روزانه",
            evidence="کار میکند",
            source_url="https://vandiyar.ir/",
            confidence=0.9,
        )],
    )
    validate_knowledge_evidence(payload, [doc])

    paraphrased = PlatformKnowledgePayload(
        summary="",
        products_and_services=[PlatformKnowledgeFact(
            name="ادعا",
            description="چیزی که در متن نیست",
            evidence="پشتیبانی ۲۴ ساعته تلفنی",
            source_url="https://vandiyar.ir/",
            confidence=0.9,
        )],
    )
    with pytest.raises(PlatformKnowledgeExtractionError, match="ادعا"):
        validate_knowledge_evidence(paraphrased, [doc])
