"""Optional Playwright automation for authorised authenticated browser sessions."""
from __future__ import annotations

from urllib.parse import urldefrag

from app.integrations.platform_discovery.public_sources import origin, validate_public_url
from app.integrations.platform_discovery.types import DiscoveredPage, DiscoveryResult


class BrowserAutomationCrawler:
    async def crawl(
        self,
        base_url: str,
        *,
        max_pages: int = 15,
        browser_config: dict | None = None,
        credentials: dict | None = None,
    ) -> DiscoveryResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Install browser extra and Chromium: "
                "pip install -e '.[browser]' && playwright install chromium"
            ) from exc

        login = (browser_config or {}).get("login", {})
        site_origin = origin(base_url)
        validate_public_url(base_url, site_origin)
        pages, warnings, queue, seen = [], [], [base_url], set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="VandadAgentBrowser/0.1",
                accept_downloads=False,
            )

            async def guard_request(route):
                try:
                    validate_public_url(route.request.url, site_origin)
                except ValueError:
                    await route.abort()
                    return
                await route.continue_()

            await context.route("**/*", guard_request)
            page = await context.new_page()
            try:
                await page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
                if login.get("username_selector") or login.get("password_selector"):
                    if (
                        not credentials
                        or not credentials.get("username")
                        or not credentials.get("password")
                    ):
                        raise ValueError(
                            "Browser login requires an authorised encrypted BASIC_AUTH credential."
                        )
                    await page.locator(login["username_selector"]).fill(credentials["username"])
                    await page.locator(login["password_selector"]).fill(credentials["password"])
                    await page.locator(login["submit_selector"]).click()
                    await page.wait_for_load_state("domcontentloaded", timeout=30000)
                    expected = login.get("success_url_contains")
                    if expected and expected not in page.url:
                        raise RuntimeError(
                            "Browser login did not reach the configured success URL."
                        )

                queue.insert(0, page.url)
                while queue and len(pages) < min(max_pages, 25):
                    current = urldefrag(queue.pop(0)).url
                    if current in seen or origin(current) != site_origin:
                        continue
                    validate_public_url(current, site_origin)
                    seen.add(current)
                    try:
                        await page.goto(current, wait_until="domcontentloaded", timeout=30000)
                        title = (await page.title())[:1000] or None
                        body = (await page.locator("body").inner_text(timeout=10000))[:100_000]
                        links = await page.locator("a[href]").evaluate_all(
                            "(nodes) => nodes.map((node) => node.href)"
                        )
                        if body.strip():
                            pages.append(
                                DiscoveredPage(
                                    url=current,
                                    title=title,
                                    text=body,
                                    source_type="browser",
                                    status_code=200,
                                    metadata={"authenticated": bool(credentials)},
                                )
                            )
                        for link in links:
                            if (
                                origin(link) == site_origin
                                and link not in seen
                                and link not in queue
                            ):
                                queue.append(link)
                    except Exception as exc:
                        warnings.append(
                            f"Browser skipped {current}: {type(exc).__name__}"
                        )
            finally:
                await context.close()
                await browser.close()
        return DiscoveryResult(pages=pages, warnings=warnings)
