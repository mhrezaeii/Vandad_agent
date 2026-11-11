"""Provider-native video finalization tests; no local renderer is required."""

from app.services.subtitle_service import build_ass_for_script, build_segments


PERSIAN_SCRIPT = (
    "شبکه سازمان شما هر ثانیه رویداد امنیتی تولید می‌کند. "
    "پایش متمرکز یعنی دیدن همه این رویدادها در یک نقطه. "
    "Vandefence این تصویر را شفاف می‌کند."
)


def test_persian_subtitle_prompt_material_is_available_to_ai_providers():
    segments = build_segments(PERSIAN_SCRIPT, total_seconds=30)
    assert len(segments) >= 2
    ass = build_ass_for_script(
        PERSIAN_SCRIPT,
        total_seconds=30,
        width=1080,
        height=1920,
        font_name="Vazirmatn",
        font_size=56,
    )
    assert "Vazirmatn" in ass
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "شبکه" in ass
