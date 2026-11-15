#!/usr/bin/env python3
"""Google Flow (labs.google/flow) free-quota clip worker — Windows 11 VPS edition.

Runs on the Nesta Windows VPS (foreign IP -> Flow accessible). One company
Google account, one persistent browser profile (login once interactively),
daily free quota. No multi-account rotation.

Commands:
  login      open a visible browser -> log into labs.google/flow once -> profile saved
  status     show session + quota state
  generate   generate one clip per prompt block in the prompt file
  prompt     generate a single clip for one prompt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
PROFILE_DIR = STATE_DIR / "chrome-profile"
OUTPUT_DIR = BASE_DIR / "clips"
SHOTS_DIR = STATE_DIR / "shots"
QUOTA_FILE = STATE_DIR / "quota.json"

DAILY_LIMIT = int(__import__("os").environ.get("FLOW_DAILY_LIMIT", "6"))
GENERATE_URL = "https://labs.google/flow"
HEADLESS = __import__("os").environ.get("FLOW_HEADLESS", "0") == "1"

# Flow's DOM changes without notice — every selector lives here.
SELECTORS = {
    "prompt_box": "textarea, [contenteditable='true'], [role='textbox']",
    "submit": "button:has-text('Create'), button:has-text('Generate'), "
              "[aria-label*='Generate'], button[type='submit']",
    "video_element": "video",
}


def quota_read() -> dict:
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        if data.get("date") == datetime.now(UTC).date().isoformat():
            return data
    except (OSError, ValueError):
        pass
    return {"date": datetime.now(UTC).date().isoformat(), "used": 0}


def quota_consume() -> None:
    data = quota_read()
    data["used"] = int(data.get("used", 0)) + 1
    QUOTA_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUOTA_FILE.write_text(json.dumps(data), encoding="utf-8")


def quota_remaining() -> int:
    return max(0, DAILY_LIMIT - quota_read()["used"])


async def _launch(force_headless: bool = False):
    from playwright.async_api import async_playwright

    manager = await async_playwright().start()
    browser = await manager.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=force_headless or HEADLESS,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
    )
    return manager, browser


async def login_cmd() -> None:
    """Open a visible browser; the operator logs into Flow once; profile saved."""
    from playwright.async_api import async_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("opening a visible browser — log into labs.google/flow with the "
          "company Google account, wait until the Flow dashboard loads, "
          "then press Enter here...")
    manager = await async_playwright().start()
    browser = await manager.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=False,
        viewport={"width": 1280, "height": 900}, locale="en-US",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = await browser.new_page()
    await page.goto(GENERATE_URL, wait_until="domcontentloaded", timeout=90_000)
    input("\n>>> after the Flow dashboard is visible, press Enter to save the profile... ")
    await browser.close()
    await manager.stop()
    print(f"profile saved: {PROFILE_DIR}")
    await asyncio.to_thread(status)


async def status_cmd() -> None:
    print(json.dumps({
        "profile_present": PROFILE_DIR.is_dir(),
        "used_today": quota_read()["used"],
        "remaining_today": quota_remaining(),
        "daily_limit": DAILY_LIMIT,
        "headless": HEADLESS,
    }, indent=2))


def _load_prompts(prompt_file: str | None, single_prompt: str | None) -> list[str]:
    if single_prompt:
        return [single_prompt]
    raw = Path(prompt_file or "").read_text(encoding="utf-8")
    prompts: list[str] = []
    for block in re.split(r"\n-{3,}\n|\n=====*\n", raw):
        text = re.sub(r"^CLIP \d+[^\n]*\n?", "", block.strip()).strip()
        if len(text) > 60:
            prompts.append(text)
    return prompts


async def _dismiss_popups(page) -> None:
    for _ in range(4):
        dismissed = False
        for sel in ("[aria-label='Close']", "button:has-text('Accept')",
                    "button:has-text('Got it')", "button:has-text('I agree')"):
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=3000)
                    dismissed = True
                    await page.wait_for_timeout(900)
                    break
            except Exception:  # noqa: BLE001
                continue
        if not dismissed:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(700)
            break


async def _generate_one(page, prompt: str, out_path: Path, shots: Path, index: int) -> None:
    await page.goto(GENERATE_URL, wait_until="domcontentloaded", timeout=90_000)
    await page.wait_for_timeout(5000)
    if "accounts.google.com" in page.url:
        raise RuntimeError("session signed out — run `flow_worker.py login` again")
    await _dismiss_popups(page)
    await page.screenshot(path=str(shots / f"flow-{index:02d}-a-landing.png"))

    box = page.locator(SELECTORS["prompt_box"]).first
    await box.wait_for(state="visible", timeout=45_000)
    await box.click()
    await page.keyboard.insert_text(prompt.strip())
    await page.wait_for_timeout(1200)
    await page.screenshot(path=str(shots / f"flow-{index:02d}-b-prompt.png"))

    submitted = False
    try:
        loc = page.locator(SELECTORS["submit"])
        for i in range(await loc.count()):
            btn = loc.nth(i)
            if await btn.is_visible() and await btn.is_enabled():
                await btn.click()
                submitted = True
                break
    except Exception:  # noqa: BLE001
        pass
    if not submitted:
        await page.keyboard.press("Control+Enter")
    print(f"  [{index}] submitted, generating (1-4 minutes)...")

    video = page.locator(SELECTORS["video_element"]).last
    await video.wait_for(state="attached", timeout=600_000)
    for _ in range(120):
        ready = await page.evaluate(
            "() => { const v = [...document.querySelectorAll('video')].pop();"
            " return v ? (v.readyState >= 3 && v.duration > 0) : false; }"
        )
        if ready:
            break
        await page.wait_for_timeout(5_000)
    await page.wait_for_timeout(2500)
    await page.screenshot(path=str(shots / f"flow-{index:02d}-c-result.png"))

    downloaded = False
    for sel in ("text=Download", "[aria-label*='Download']", "button:has-text('Download')"):
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.last.is_visible():
                async with page.expect_download(timeout=180_000) as dl_info:
                    await loc.last.click()
                download = await dl_info.value
                await download.save_as(str(out_path))
                downloaded = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not downloaded:
        src = await page.evaluate(
            "() => { const v = [...document.querySelectorAll('video')].pop();"
            " return v ? (v.src || v.querySelector('source')?.src || null) : null; }"
        )
        if src:
            import httpx

            cookies = await page.context.cookies()
            with httpx.Client(cookies={c["name"]: c["value"] for c in cookies},
                              timeout=180, follow_redirects=True) as client:
                r = client.get(src)
                if r.status_code == 200 and len(r.content) > 10_000:
                    out_path.write_bytes(r.content)
                    downloaded = True
    if not downloaded or not out_path.is_file() or out_path.stat().st_size < 10_000:
        await page.screenshot(path=str(shots / f"flow-{index:02d}-d-failed.png"))
        raise RuntimeError("download failed — inspect the saved screenshots")
    print(f"  [{index}] saved: {out_path} ({out_path.stat().st_size:,} bytes)")


async def generate_cmd(prompt_file: str | None, single_prompt: str | None, out_dir: str) -> None:
    prompts = _load_prompts(prompt_file, single_prompt)
    if not prompts:
        print("ERROR: no prompts found.", file=sys.stderr)
        sys.exit(2)
    if quota_remaining() < len(prompts):
        print(f"NOTE: {len(prompts)} prompts but only {quota_remaining()} quota left today; "
              f"generating as many as possible.")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shots = SHOTS_DIR / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    shots.mkdir(parents=True, exist_ok=True)

    manager, browser = await _launch(force_headless=False)
    page = await browser.new_page()
    start_index = 1 + sum(1 for _ in out.glob("*.mp4"))
    ok = failed = 0
    try:
        for offset, prompt in enumerate(prompts):
            index = start_index + offset
            if quota_remaining() <= 0:
                print(f"  [{index}] daily quota exhausted — rerun tomorrow.")
                break
            out_path = out / f"{index:02d}.mp4"
            try:
                await _generate_one(page, prompt, out_path, shots, index)
                quota_consume()
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [{index}] FAILED: {str(exc)[:180]}", file=sys.stderr)
    finally:
        await browser.close()
        await manager.stop()
    print(f"done: ok={ok} failed={failed} | clips in {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("login")
    sub.add_parser("status")
    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--prompt-file", default="prompts-vandiyar.txt")
    p_gen.add_argument("--out", default=str(OUTPUT_DIR))
    p_gen.add_argument("--prompt", default=None)
    p_one = sub.add_parser("prompt")
    p_one.add_argument("text")
    args = parser.parse_args()

    if args.cmd == "login":
        asyncio.run(login_cmd())
    elif args.cmd == "status":
        asyncio.run(status_cmd())
    elif args.cmd == "generate":
        asyncio.run(generate_cmd(args.prompt_file, args.prompt, args.out))
    elif args.cmd == "prompt":
        asyncio.run(generate_cmd(None, args.text, str(OUTPUT_DIR)))
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
