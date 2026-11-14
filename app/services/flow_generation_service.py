"""Single-account Google Flow / AI Studio free-quota video worker.

Company policy (deliberate design boundary): this worker drives **one**
company-owned Google account and its own daily free quota. Multi-account
rotation is intentionally not implemented — when the volume exceeds one
account's quota, the pipeline falls back to the paid WaveSpeed flash path.

Mechanics:
- The Google session lives in a Playwright storage-state JSON
  (``FLOW_STORAGE_STATE``). The operator logs in once (cookie export or a
  headed first run) and the session is reused until it expires.
- Each generation consumes one unit of the daily quota (tracked in a small
  JSON counter next to the state file); when the quota is exhausted the
  service raises :class:`FlowQuotaExhausted` so callers can fall back.
- AI Studio's DOM changes without notice; every selector lives in
  ``SELECTORS`` and failures save a full-page screenshot next to the state
  file for live debugging.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("app.flow")


class FlowError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class FlowQuotaExhausted(FlowError):
    def __init__(self) -> None:
        super().__init__(
            "The Google account's daily free video quota is exhausted; "
            "fall back to the paid provider until tomorrow.",
            code="flow_quota_exhausted",
        )


class FlowSessionMissing(FlowError):
    def __init__(self, state_path: str) -> None:
        super().__init__(
            "No Google session found; import cookies into "
            f"{state_path} (scripts/import_flow_cookies.py) before generating.",
            code="flow_session_missing",
        )


@dataclass(frozen=True)
class FlowSettings:
    enabled: bool
    daily_limit: int
    storage_state: Path
    profile_dir: Path
    chromium_path: Path
    headless: bool
    timeout_seconds: int

    @classmethod
    def from_config(cls, config: object) -> "FlowSettings":  # noqa: ANN001 - Settings
        return cls(
            enabled=bool(getattr(config, "flow_enabled", False)),
            daily_limit=int(getattr(config, "flow_daily_limit", 3)),
            storage_state=Path(str(getattr(config, "flow_storage_state", ""))),
            profile_dir=Path(str(getattr(config, "flow_profile_dir", ""))),
            chromium_path=Path(str(getattr(config, "flow_chromium_path", ""))),
            headless=bool(getattr(config, "flow_headless", True)),
            timeout_seconds=int(getattr(config, "flow_timeout_seconds", 900)),
        )


# AI Studio video-generation selectors (single place to update when the UI moves).
SELECTORS = {
    "media_tab": "text=Generate media",
    "video_tool": "text=Video",
    "prompt_box": "textarea",
    "aspect_916": "text=9:16",
    "submit": "button:has-text('Run')",
    "download": "text=Download",
}


class QuotaTracker:
    """Tiny JSON counter: {date, used} reset at UTC midnight."""

    def __init__(self, state_path: Path, daily_limit: int) -> None:
        self._path = state_path.with_name("quota.json")
        self._limit = daily_limit

    def used_today(self) -> int:
        data = self._read()
        return data["used"] if data["date"] == self._today() else 0

    def remaining(self) -> int:
        return max(0, self._limit - self.used_today())

    def consume(self) -> None:
        used = self.used_today() + 1
        self._write({"date": self._today(), "used": used})

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "date" in data and "used" in data:
                return data
        except (OSError, ValueError):
            pass
        return {"date": self._today(), "used": 0}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()


def convert_cookie_export(export: list[dict], *, output_path: Path) -> Path:
    """Convert a Cookie-Editor JSON export into a Playwright storage state.

    Only google.com cookies are kept; sameSite values are normalized to what
    Playwright accepts.
    """
    cookies: list[dict] = []
    allowed_sites = {"sameSite_lax", "sameSite_strict", "sameSite_none"}
    for item in export:
        domain = str(item.get("domain") or "")
        if "google.com" not in domain:
            continue
        same_site = str(item.get("sameSite") or "lax").lower()
        cookie = {
            "name": str(item.get("name") or ""),
            "value": str(item.get("value") or ""),
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
        }
        if cookie["name"].startswith("__Host-"):
            cookie["sameSite"] = "Strict"
        elif same_site in allowed_sites:
            cookie["sameSite"] = same_site.removeprefix("sameSite_").replace("_", "-") or "Lax"
        else:
            cookie["sameSite"] = "Lax"
        expires = item.get("expirationDate") or item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = int(expires)
        cookies.append(cookie)
    if not cookies:
        raise FlowError(
            "The cookie export contained no google.com cookies.", code="flow_cookie_export_empty"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2), encoding="utf-8"
    )
    return output_path


class FlowGenerationService:
    """Drive one AI Studio Veo generation with the stored company session."""

    def __init__(self, settings: FlowSettings) -> None:
        self.settings = settings
        self.quota = QuotaTracker(settings.storage_state, settings.daily_limit)

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "session_present": self.settings.storage_state.is_file(),
            "used_today": self.quota.used_today(),
            "remaining_today": self.quota.remaining(),
        }

    async def generate(self, prompt: str, *, aspect_ratio: str = "9:16") -> Path:
        """Generate one video and return the downloaded MP4 path."""
        if not self.settings.enabled:
            raise FlowError("The Flow worker is disabled.", code="flow_disabled")
        if not self.settings.storage_state.is_file():
            raise FlowSessionMissing(str(self.settings.storage_state))
        if self.quota.remaining() <= 0:
            raise FlowQuotaExhausted()
        if not prompt.strip():
            raise FlowError("A prompt is required.", code="flow_empty_prompt")

        from playwright.async_api import async_playwright  # noqa: PLC0415 - heavy import

        output_dir = self.settings.storage_state.parent / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = output_dir / f"flow-{stamp}.mp4"

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.headless,
                executable_path=str(self.settings.chromium_path)
                if self.settings.chromium_path.is_file() else None,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                storage_state=str(self.settings.storage_state),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            page = await context.new_page()
            deadline = __import__("time").monotonic() + self.settings.timeout_seconds
            try:
                await page.goto("https://aistudio.google.com/generate-media", wait_until="domcontentloaded")
                if "accounts.google.com" in page.url:
                    raise FlowError(
                        "The stored session is signed out; re-import fresh cookies.",
                        code="flow_session_expired",
                    )
                for key in ("media_tab", "video_tool"):
                    locator = page.locator(SELECTORS[key]).first
                    await locator.wait_for(state="visible", timeout=30_000)
                    await locator.click()
                box = page.locator(SELECTORS["prompt_box"]).first
                await box.wait_for(state="visible", timeout=30_000)
                await box.fill(prompt.strip())
                if aspect_ratio == "9:16":
                    try:
                        await page.locator(SELECTORS["aspect_916"]).first.click(timeout=8_000)
                    except Exception:  # noqa: BLE001 - aspect control may be a dropdown
                        logger.info("aspect selector not found; continuing with default")
                await page.locator(SELECTORS["submit"]).first.click()

                await page.locator(SELECTORS["download"]).first.wait_for(
                    state="visible", timeout=self._remaining_ms(deadline)
                )
                async with page.expect_download(timeout=self._remaining_ms(deadline)) as download_info:
                    await page.locator(SELECTORS["download"]).first.click()
                download = await download_info.value
                await download.save_as(str(target))
            except Exception as exc:
                shot = self.settings.storage_state.parent / f"flow-failure-{stamp}.png"
                try:
                    await page.screenshot(path=str(shot), full_page=True)
                    logger.error("Flow generation failed; screenshot: %s (%s)", shot, exc)
                except Exception:  # noqa: BLE001
                    logger.error("Flow generation failed and screenshot also failed: %s", exc)
                if isinstance(exc, FlowError):
                    raise
                raise FlowError(
                    "AI Studio generation failed; inspect the saved screenshot.",
                    code="flow_generation_failed",
                ) from exc
            finally:
                await context.close()
                await browser.close()

        if not target.is_file() or target.stat().st_size < 10_000:
            target.unlink(missing_ok=True)
            raise FlowError("The downloaded video is missing or empty.", code="flow_empty_output")
        self.quota.consume()
        return target

    @staticmethod
    def _remaining_ms(deadline: float) -> float:
        return max(1_000.0, (deadline - __import__("time").monotonic()) * 1000)
