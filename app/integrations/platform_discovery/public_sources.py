"""Bounded same-origin public-source crawler with robots and SSRF safeguards."""
from __future__ import annotations
import ipaddress
import socket
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser
import httpx
from app.integrations.platform_discovery.types import DiscoveredPage, DiscoveryResult

USER_AGENT = "VandadAgentBot/0.1 (+authorized platform discovery)"
KEYWORDS = ("docs", "document", "guide", "help", "support", "changelog", "release", "feature", "product", "faq")

class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts, self.text_parts, self.links = [], [], []
        self.in_title, self.skip_depth = False, 0
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title": self.in_title = True
        if tag in {"script", "style", "noscript", "svg"}: self.skip_depth += 1
        if tag == "a" and values.get("href"): self.links.append(values["href"])
    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth: self.skip_depth -= 1
    def handle_data(self, data):
        value = " ".join(data.split())
        if not value: return
        if self.in_title: self.title_parts.append(value)
        if not self.skip_depth: self.text_parts.append(value)

def origin(url: str):
    parsed = urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)

def validate_public_url(url: str, expected_origin=None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed.")
    if expected_origin is not None and origin(url) != expected_origin:
        raise ValueError("Cross-origin navigation is not allowed.")
    host = parsed.hostname
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))}
        except OSError as exc:
            raise ValueError("The target hostname could not be resolved.") from exc
    if not addresses or any(a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified for a in addresses):
        raise ValueError("Private or reserved network destinations are not allowed.")
    return url

def normalise_url(url: str) -> str:
    parsed = urlparse(urldefrag(url).url)
    return parsed._replace(fragment="", path=parsed.path or "/").geturl()

def parse_html(url: str, body: str, status: int):
    parser = PageParser()
    parser.feed(body)
    page = DiscoveredPage(url=url, title=" ".join(parser.title_parts).strip() or None,
        text=" ".join(parser.text_parts)[:100_000], source_type="website",
        status_code=status, metadata={"content_type": "text/html"})
    return page, parser.links

class PublicSourceCrawler:
    def __init__(self, timeout_seconds=15.0, max_bytes=2_000_000):
        self.timeout_seconds, self.max_bytes = timeout_seconds, max_bytes

    async def _get(self, client, url, expected_origin):
        current = url
        for _ in range(4):
            validate_public_url(current, expected_origin)
            response = await client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location: return response
                current = urljoin(current, location)
                continue
            if len(response.content) > self.max_bytes: raise ValueError("Response exceeded configured size limit.")
            return response
        raise ValueError("Too many redirects.")

    async def crawl(self, base_url: str, *, max_pages=25) -> DiscoveryResult:
        base, site_origin = normalise_url(base_url), origin(base_url)
        validate_public_url(base, site_origin)
        warnings, pages = [], []
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,text/plain;q=0.9,*/*;q=0.5"}
        async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=headers, follow_redirects=False,
                limits=httpx.Limits(max_connections=5, max_keepalive_connections=2)) as client:
            robots_url = urljoin(base, "/robots.txt")
            robots = RobotFileParser()
            try:
                robots_response = await self._get(client, robots_url, site_origin)
                robots_text = robots_response.text if robots_response else ""
                robots.parse(robots_text.splitlines())
            except (httpx.HTTPError, ValueError):
                robots_text = ""
                robots.parse([])
                warnings.append("robots.txt could not be read; conservative bounded crawl continued.")
            sitemap_urls = [urljoin(base, "/sitemap.xml")]
            sitemap_urls.extend(line.split(":", 1)[1].strip() for line in robots_text.splitlines()
                                 if line.lower().startswith("sitemap:") and ":" in line)
            queue = deque([base])
            for sitemap in dict.fromkeys(sitemap_urls):
                try:
                    response = await self._get(client, sitemap, site_origin)
                    if response and response.status_code == 200:
                        root = ET.fromstring(response.text[:self.max_bytes])
                        for node in root.iter():
                            if node.tag.rsplit("}", 1)[-1] == "loc" and node.text:
                                candidate = normalise_url(urljoin(base, node.text.strip()))
                                if origin(candidate) == site_origin: queue.append(candidate)
                except (httpx.HTTPError, ValueError, ET.ParseError):
                    continue
            seen = set()
            while queue and len(pages) < max_pages:
                url = normalise_url(queue.popleft())
                if url in seen or origin(url) != site_origin: continue
                seen.add(url)
                try:
                    validate_public_url(url, site_origin)
                    if not robots.can_fetch(USER_AGENT, url): continue
                    response = await self._get(client, url, site_origin)
                except (httpx.HTTPError, ValueError) as exc:
                    warnings.append(f"Skipped {url}: {type(exc).__name__}")
                    continue
                if response is None or response.status_code >= 400: continue
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type and "application/xhtml" not in content_type: continue
                page, links = parse_html(url, response.text, response.status_code)
                if page.text: pages.append(page)
                links = sorted((urljoin(url, link) for link in links if link),
                    key=lambda item: (not any(k in item.lower() for k in KEYWORDS), len(item)))
                for link in links:
                    candidate = normalise_url(link)
                    if origin(candidate) == site_origin and candidate not in seen: queue.append(candidate)
            return DiscoveryResult(pages=pages, warnings=warnings)
