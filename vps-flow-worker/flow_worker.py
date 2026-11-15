#!/usr/bin/env python3
"""Google Flow (labs.google/flow) free-quota clip worker — VPS edition.

Runs on a foreign VPS (Flow is geo-blocked from Iran). One company Google
account, one cookie session, daily free quota. No multi-account rotation.

Setup (see README.md):
  1. bash install.sh          # python + playwright + chromium
  2. python flow_worker.py import-cookies cookies.json
  3. python flow_worker.py generate --prompt-file prompts.txt --out clips/

Commands:
  import-cookies <file>   import a Cookie-Editor JSON export (labs.google)
  status                  show session + quota state
  generate                generate clips for every prompt in the file
  prompt <text>           generate one clip for a single prompt
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
OUTPUT_DIR = BASE_DIR / "clips"
SHOTS_DIR = STATE_DIR / "shots"
STORAGE_STATE = STATE_DIR / "storage_state.json"
PROFILE_DIR = STATE_DIR / "profile"
QUOTA_FILE = STATE_DIR / "quota.json"

DAILY_LIMIT = int(__import__("os").environ.get("FLOW_DAILY_LIMIT", "6"))
GENERATE_URL = "https://labs.google/flow"
TIMEOUT_SECONDS = int(__import__("os").environ.get("FLOW_TIMEOUT_SECONDS", "1200"))

# Flow's DOM changes without notice — every selector lives here.
SELECTORS = {
    "create_button": "button:has-text('Create'), a:has-text('Create'), [aria-label*='Create']",
    "prompt_box": "textarea, [contenteditable='true'], [role='textbox']",
    "submit": "button:has-text('Create'), button:has-text('Generate'), [aria-label*='Generate'], button[type='submit']",
    "video_element": "video",
    "download": "text=Download",
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


def import_cookies(export_path: str) -> None:
    export = json.loads(Path(export_path).read_text(encoding="utf-8"))
    if isinstance(export, dict):
        export = export.get("cookies", [])
    cookies = []
    for item in export:
        domain = str(item.get("domain") or "")
        if not any(d in domain for d in ("google.com", "labs.google", "accounts.google.com")):
            continue
        cookie = {
            "name": str(item.get("name") or ""),
            "value": str(item.get("value") or ""),
            "domain": domain,
            "path": str(item.get("path") or "/"),
            "httpOnly": bool(item.get("httpOnly", False)),
            "secure": bool(item.get("secure", True)),
        }
        expires = item.get("expirationDate") or item.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = int(expires)
        cookies.append(cookie)
    if not cookies:
        print("ERROR: the export contained no google/labs.google cookies.", file=sys.stderr)
        sys.exit(2)
    STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_STATE.write_text(
        json.dumps({"cookies": cookies, "origins": []}, indent=2), encoding="utf-8"
    )
    print(f"session imported: {len(cookies)} cookies -> {STORAGE_STATE}")


def status() -> None:
    print(json.dumps({
        "session_present": STORAGE_STATE.is_file(),
        "used_today": quota_read()["used"],
        "remaining_today": quota_remaining(),
        "daily_limit": DAILY_LIMIT,
    }, indent=2))


async def _dismiss_popups(page) -> None:
    for _ in range(4):
        dismissed = False
        for sel in ("[aria-label='Close']", "button:has-text('Accept')", "button:has-text('Got it')",
                    "button:has-text('I agree')", "[class*='close']"):
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
    await page.wait_for_timeout(6000)
    if "accounts.google.com" in page.url:
        raise RuntimeError("session signed out — re-import fresh cookies")
    await _dismiss_popups(page)
    await page.screenshot(path=str(shots / f"flow-{index:02d}-a-landing.png"))

    box = page.locator(SELECTORS["prompt_box"]).first
    await box.wait_for(state="visible", timeout=45_000)
    await box.click()
    await page.keyboard.insert_text(prompt.strip())
    await page.wait_for_timeout(1200)
    await page.screenshot(path=str(shots / f"flow-{index:02d}-b-prompt.png"))

    submitted = False
    for sel in (SELECTORS["submit"],):
        try:
            loc = page.locator(sel)
            for i in range(await loc.count()):
                btn = loc.nth(i)
                if await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    submitted = True
                    break
            if submitted:
                break
        except Exception:  # noqa: BLE001
            continue
    if not submitted:
        await page.keyboard.press("Control+Enter")
        submitted = True
    print(f"  [{index}] submitted, generating (this takes 1-4 minutes)...")

    # wait for a video element to appear in the latest generation card
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
    for sel in (SELECTORS["download"], "[aria-label*='Download']", "button:has-text('Download')"):
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
        # fallback: grab the video src and fetch it via the browser context
        src = await page.evaluate(
            "() => { const v = [...document.querySelectorAll('video')].pop();"
            " return v ? (v.src || v.querySelector('source')?.src || null) : null; }"
        )
        if src:
            import httpx

            cookies = await page.context.cookies()
            with httpx.Client(cookies={c["name"]: c["value"] for c in cookies}, timeout=180,
                              follow_redirects=True) as client:
                r = client.get(src)
                if r.status_code == 200 and len(r.content) > 10_000:
                    out_path.write_bytes(r.content)
                    downloaded = True
    if not downloaded or not out_path.is_file() or out_path.stat().st_size < 10_000:
        await page.screenshot(path=str(shots / f"flow-{index:02d}-d-failed.png"))
        raise RuntimeError("download failed — inspect the saved screenshots")
    print(f"  [{index}] saved: {out_path} ({out_path.stat().st_size:,} bytes)")


async def generate_cmd(prompt_file: str | None, single_prompt: str | None, out_dir: str) -> None:
    from playwright.async_api import async_playwright

    prompts: list[str] = []
    if single_prompt:
        prompts = [single_prompt]
    else:
        raw = Path(prompt_file or "").read_text(encoding="utf-8")
        for block in re.split(r"\n-{3,}\n|\n=====*\n|\n\n(?=CLIP \d+)", raw):
            text = block.strip()
            text = re.sub(r"^CLIP \d+[^\n]*\n?", "", text).strip()
            if len(text) > 60:
                prompts.append(text)
    if not prompts:
        print("ERROR: no prompts found.", file=sys.stderr)
        sys.exit(2)

    if quota_remaining() < len(prompts):
        print(f"WARNING: {len(prompts)} prompts but only {quota_remaining()} quota left today; "
              f"generating as many as possible.")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shots = SHOTS_DIR / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    shots.mkdir(parents=True, exist_ok=True)

    start_index = 1 + sum(1 for _ in out.glob("*.mp4"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            storage_state=str(STORAGE_STATE) if STORAGE_STATE.is_file() else None,
            viewport={"width": 1440, "height": 900}, locale="en-US",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = await context.new_page()
        ok = failed = 0
        for offset, prompt in enumerate(prompts):
            index = start_index + offset
            if quota_remaining() <= 0:
                print(f"  [{index}] daily quota exhausted — stop here, rerun tomorrow.")
                break
            out_path = out / f"{index:02d}.mp4"
            try:
                await _generate_one(page, prompt, out_path, shots, index)
                quota_consume()
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [{index}] FAILED: {str(exc)[:160]}", file=sys.stderr)
        await context.close()
        await browser.close()
    print(f"done: ok={ok} failed={failed} | clips in {out}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    p_imp = sub.add_parser("import-cookies")
    p_imp.add_argument("file")
    sub.add_parser("status")
    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--prompt-file", default="prompts.txt")
    p_gen.add_argument("--out", default=str(OUTPUT_DIR))
    p_gen.add_argument("--prompt", default=None, help="generate a single prompt")
    p_one = sub.add_parser("prompt")
    p_one.add_argument("text")
    args = parser.parse_args()

    if args.cmd == "import-cookies":
        import_cookies(args.file)
    elif args.cmd == "status":
        status()
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
