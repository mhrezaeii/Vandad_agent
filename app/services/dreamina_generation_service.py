"""Dreamina (CapCut/ByteDance) free-tier Seedance worker.

Single company Google account, ~120 credits/day (Seedance 2.0 Mini,
~0.55 credits per 5s generation). Uses a persistent Playwright profile so
promo carousels and consent screens stay dismissed between runs, and routes
through the operator's VPN relay (geo-requirements).

Status: experimental — the site's DOM changes frequently; every run saves
stage screenshots next to the profile for live debugging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.services.flow_generation_service import QuotaTracker

logger = logging.getLogger("app.dreamina")


class DreaminaError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DreaminaSettings:
    enabled: bool
    daily_limit: int
    storage_state: Path
    profile_dir: Path
    chromium_path: Path
    headless: bool
    timeout_seconds: int
    proxy: str = ""

    @classmethod
    def from_config(cls, config: object) -> "DreaminaSettings":  # noqa: ANN001 - Settings
        return cls(
            enabled=bool(getattr(config, "dreamina_enabled", False)),
            daily_limit=int(getattr(config, "dreamina_daily_limit", 6)),
            storage_state=Path(str(getattr(config, "dreamina_storage_state", ""))),
            profile_dir=Path(str(getattr(config, "dreamina_profile_dir", ""))),
            chromium_path=Path(str(getattr(config, "dreamina_chromium_path", "/usr/bin/chromium"))),
            headless=bool(getattr(config, "dreamina_headless", True)),
            timeout_seconds=int(getattr(config, "dreamina_timeout_seconds", 1200)),
            proxy=str(getattr(config, "dreamina_proxy", "") or ""),
        )


class DreaminaGenerationService:
    def __init__(self, settings: DreaminaSettings) -> None:
        self.settings = settings
        self.quota = QuotaTracker(settings.storage_state, settings.daily_limit)

    def status(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "session_present": self.settings.storage_state.is_file(),
            "used_today": self.quota.used_today(),
            "remaining_today": self.quota.remaining(),
        }

    async def generate(self, prompt: str) -> Path:
        """Generate one 5s Seedance clip and return the downloaded MP4 path."""
        if not self.settings.enabled:
            raise DreaminaError("The Dreamina worker is disabled.", code="dreamina_disabled")
        if not self.settings.storage_state.is_file():
            raise DreaminaError(
                "No Dreamina session; import cookies first.", code="dreamina_session_missing"
            )
        if self.quota.remaining() <= 0:
            raise DreaminaError(
                "Dreamina daily quota exhausted.", code="dreamina_quota_exhausted"
            )
        if not prompt.strip():
            raise DreaminaError("A prompt is required.", code="dreamina_empty_prompt")

        from playwright.async_api import async_playwright  # noqa: PLC0415

        output_dir = self.settings.storage_state.parent / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        shots = self.settings.storage_state.parent / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"dreamina-{stamp}.mp4"

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.headless,
                executable_path=str(self.settings.chromium_path)
                if self.settings.chromium_path.is_file() else None,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-blink-features=AutomationControlled"],
            )
            common = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "en-US",
                "proxy": {"server": self.settings.proxy} if self.settings.proxy else None,
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            context = await browser.new_context(storage_state=str(self.settings.storage_state), **common)
            page = await context.new_page()
            deadline = __import__("time").monotonic() + self.settings.timeout_seconds
            try:
                await page.goto("https://dreamina.capcut.com/ai-tool/ai-video",
                                wait_until="domcontentloaded", timeout=60_000)
                await page.wait_for_timeout(7000)
                # dismiss promo carousels / modals: the X button sits at the
                # top-right of the promo card on every page variant.
                for _ in range(6):
                    if not await page.locator('[aria-label="Prompt"]').first.is_visible():
                        await page.mouse.click(1140, 196)
                        await page.wait_for_timeout(1400)
                        continue
                    # modal still covering the editor? click through positions
                    editor_box = await page.locator('[aria-label="Prompt"]').first.bounding_box()
                    if editor_box and editor_box["y"] > 100:
                        break
                    await page.mouse.click(1140, 196)
                    await page.wait_for_timeout(1400)
                editor = page.locator('[aria-label="Prompt"]').first
                await editor.wait_for(state="visible", timeout=20_000)
                await editor.click(force=True)
                await page.keyboard.insert_text(prompt.strip())
                await page.wait_for_timeout(1200)
                typed = len((await editor.inner_text()).strip())
                logger.info("dreamina: prompt typed, editor len=%d", typed)
                await page.screenshot(path=str(shots / "dreamina-filled.png"))
                # promo carousels re-appear mid-session; dismiss again right
                # before submitting (the X is at the top-right of the card).
                for _ in range(3):
                    await page.mouse.click(1140, 196)
                    await page.wait_for_timeout(1200)
                if len((await editor.inner_text()).strip()) < 50:
                    await editor.click(force=True)
                    await page.keyboard.insert_text(prompt.strip())
                    await page.wait_for_timeout(1200)
                await page.screenshot(path=str(shots / "dreamina-prerun.png"))
                # submit via keyboard (the arrow button sits outside the
                # editor's own bounding box and the promo keeps re-covering it)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(4_000)
                await page.screenshot(path=str(shots / "dreamina-postsubmit.png"))
                logger.info("dreamina: submit attempted via Enter")
                # wait for the generation result card / download affordance
                downloaded = False
                while __import__("time").monotonic() < deadline and not downloaded:
                    await page.wait_for_timeout(15_000)
                    # the promo carousel re-shows periodically; dismiss it
                    try:
                        promo = page.locator('text=Try it now').first
                        if await promo.count() > 0:
                            await page.mouse.click(1140, 196)
                            await page.wait_for_timeout(1_000)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        dl = page.locator('text=Download').first
                        if await dl.count() > 0 and await dl.is_visible():
                            async with page.expect_download(timeout=60_000) as dl_info:
                                await dl.click()
                            download = await dl_info.value
                            await download.save_as(str(target))
                            downloaded = True
                    except Exception:  # noqa: BLE001 - keep polling until deadline
                        continue
                    if int(__import__("time").monotonic()) % 60 < 15:
                        await page.screenshot(path=str(shots / "dreamina-polling.png"))
                if not downloaded:
                    await page.screenshot(path=str(shots / "dreamina-timeout.png"))
                    raise DreaminaError(
                        "Generation did not produce a download within the deadline.",
                        code="dreamina_generation_timeout",
                    )
            except Exception as exc:
                try:
                    await page.screenshot(path=str(shots / f"dreamina-failure-{stamp}.png"))
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(exc, DreaminaError):
                    raise
                raise DreaminaError(
                    "Dreamina automation failed; inspect saved screenshots.",
                    code="dreamina_generation_failed",
                ) from exc
            finally:
                await context.close()
                await browser.close()

        if not target.is_file() or target.stat().st_size < 10_000:
            target.unlink(missing_ok=True)
            raise DreaminaError("The downloaded clip is missing or empty.", code="dreamina_empty_output")
        self.quota.consume()
        return target
