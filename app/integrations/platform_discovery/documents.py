"""Admin-configured internal, OpenAPI and document source adapters.

These adapters are deliberately separate from the public crawler: private
network destinations are only reachable when an administrator explicitly
configures them on a platform, and no discovered link can widen that scope.
All fetches remain bounded, same-origin and HTTP(S)-only.
"""

from __future__ import annotations

import ipaddress
import socket
from collections import deque
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.integrations.platform_discovery.public_sources import (
    KEYWORDS,
    USER_AGENT,
    normalise_url,
    origin,
    parse_html,
)
from app.integrations.platform_discovery.types import DiscoveredPage, DiscoveryResult


def validate_internal_url(url: str, expected_origin=None) -> str:
    """Validate an admin-configured internal URL (private addresses allowed)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")
    if expected_origin is not None and origin(url) != expected_origin:
        raise ValueError("Cross-origin navigation is not allowed.")
    try:
        addresses = {ipaddress.ip_address(parsed.hostname)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(
                    parsed.hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                )
            }
        except OSError as exc:
            raise ValueError("The target hostname could not be resolved.") from exc
    if not addresses or any(
        item.is_link_local or item.is_multicast or item.is_reserved or item.is_unspecified
        for item in addresses
    ):
        raise ValueError("Link-local, multicast or reserved destinations are not allowed.")
    return url


class InternalSourceCrawler:
    """Bounded same-origin crawl of an explicitly configured internal environment."""

    def __init__(self, timeout_seconds=15.0, max_bytes=2_000_000):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    async def _get(self, client, url, expected_origin):
        current = url
        for _ in range(4):
            validate_internal_url(current, expected_origin)
            response = await client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return response
                current = urljoin(current, location)
                continue
            if len(response.content) > self.max_bytes:
                raise ValueError("Response exceeded configured size limit.")
            return response
        raise ValueError("Too many redirects.")

    async def crawl(self, base_url: str, *, max_pages=25) -> DiscoveryResult:
        base, site_origin = normalise_url(base_url), origin(base_url)
        validate_internal_url(base, site_origin)
        warnings: list[str] = []
        pages: list[DiscoveredPage] = []
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.5"}
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers=headers,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=2),
        ) as client:
            robots = RobotFileParser()
            try:
                robots_response = await self._get(client, urljoin(base, "/robots.txt"), site_origin)
                robots.parse((robots_response.text if robots_response else "").splitlines())
            except (httpx.HTTPError, ValueError):
                robots.parse([])
                warnings.append("robots.txt could not be read; bounded internal crawl continued.")
            queue = deque([base])
            seen: set[str] = set()
            while queue and len(pages) < max_pages:
                url = normalise_url(queue.popleft())
                if url in seen or origin(url) != site_origin:
                    continue
                seen.add(url)
                try:
                    validate_internal_url(url, site_origin)
                    if not robots.can_fetch(USER_AGENT, url):
                        continue
                    response = await self._get(client, url, site_origin)
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"Skipped {url}: {type(exc).__name__}")
                    continue
                if response is None or response.status_code >= 400:
                    warnings.append(f"Skipped {url}: HTTP {response.status_code if response else 'unknown'}")
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml" not in content_type:
                    continue
                page, links = parse_html(url, response.text, response.status_code)
                page = DiscoveredPage(
                    url=page.url, title=page.title, text=page.text,
                    source_type="internal", status_code=page.status_code,
                    metadata={**page.metadata, "internal": True},
                )
                if page.text:
                    pages.append(page)
                for link in sorted(
                    (urljoin(url, item) for item in links if item),
                    key=lambda item: (not any(k in item.lower() for k in KEYWORDS), len(item)),
                ):
                    candidate = normalise_url(link)
                    if origin(candidate) == site_origin and candidate not in seen:
                        queue.append(candidate)
        return DiscoveryResult(pages=pages, warnings=warnings)


class StructuredDocumentLoader:
    """Fetch one admin-configured OpenAPI spec or text/markdown document."""

    def __init__(self, timeout_seconds=20.0, max_bytes=2_000_000):
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes

    async def load(self, url: str, *, allow_private: bool = False) -> DiscoveredPage:
        validator = validate_internal_url if allow_private else _public_validator()
        validator(url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/markdown,text/plain,*/*;q=0.5",
        }
        current = url
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, headers=headers, follow_redirects=False
        ) as client:
            for _ in range(4):
                validator(current)
                response = await client.get(current)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        break
                    current = urljoin(current, location)
                    continue
                break
            else:
                raise ValueError("Too many redirects.")
        if response.status_code >= 400:
            raise ValueError(f"Source returned HTTP {response.status_code}.")
        if len(response.content) > self.max_bytes:
            raise ValueError("Source exceeded configured size limit.")
        content_type = response.headers.get("content-type", "").lower()
        is_json = "json" in content_type or urlparse(url).path.lower().endswith(".json")
        if is_json:
            try:
                spec = response.json()
            except ValueError as exc:
                raise ValueError("OpenAPI source is not valid JSON.") from exc
            if not isinstance(spec, dict) or "paths" not in spec:
                raise ValueError("JSON source is not an OpenAPI document (no 'paths' key).")
            text = response.text[:100_000]
            return DiscoveredPage(
                url=url, title=str((spec.get("info") or {}).get("title") or None),
                text=text, source_type="openapi", status_code=response.status_code,
                metadata={"content_type": "application/json", "openapi": True},
            )
        if "markdown" in content_type or urlparse(url).path.lower().endswith((".md", ".markdown")):
            source_type = "document"
        else:
            source_type = "document"
        text = response.text[:100_000]
        if not text.strip():
            raise ValueError("Source document is empty.")
        return DiscoveredPage(
            url=url, title=None, text=text, source_type=source_type,
            status_code=response.status_code,
            metadata={"content_type": content_type or "text/plain"},
        )


def _public_validator():
    from app.integrations.platform_discovery.public_sources import validate_public_url  # noqa: PLC0415

    return validate_public_url
