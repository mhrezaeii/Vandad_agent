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

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"script", "style", "noscript", "svg"}
_TRACK_TAGS = _HEADING_TAGS | {"li", "dt", "dd", "label", "button", "a", "nav", "form"}


def _signal_role(tag: str, ancestors: set[str]) -> str | None:
    """Map a parsed HTML element to a deterministic extraction role."""
    if tag in _HEADING_TAGS:
        return "heading"
    if tag == "li":
        return "nav" if "nav" in ancestors else "list"
    if tag in {"dt", "dd"}:
        return "faq"
    if tag in {"label", "button"}:
        return "form" if "form" in ancestors else None
    if tag == "a":
        return "nav" if "nav" in ancestors else None
    return None


class PageParser(HTMLParser):
    """Collect page text, links and structural signals for deterministic extraction."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts, self.text_parts, self.links = [], [], []
        self.signals: list[dict] = []
        self.meta_description = ""
        self.in_title, self.skip_depth = False, 0
        self._frames: list[dict] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description"} and values.get("content"):
                self.meta_description = " ".join(values["content"].split())
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag in _TRACK_TAGS:
            ancestors = {frame["tag"] for frame in self._frames}
            if tag == "a" and "nav" not in ancestors:
                return
            self._frames.append({"tag": tag, "parts": []})

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in _SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        for index in range(len(self._frames) - 1, -1, -1):
            frame = self._frames[index]
            if frame["tag"] != tag:
                continue
            self._frames = self._frames[:index]
            text = " ".join(frame["parts"])
            if len(text) >= 3:
                role = _signal_role(tag, {item["tag"] for item in self._frames})
                if role:
                    self.signals.append({"text": text[:1000], "role": role})
            break

    def handle_data(self, data):
        value = " ".join(data.split())
        if not value:
            return
        if self.in_title:
            self.title_parts.append(value)
        if not self.skip_depth:
            self.text_parts.append(value)
        for frame in self._frames:
            frame["parts"].append(value)

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
    text_parts = list(parser.text_parts)
    if parser.meta_description:
        text_parts.insert(0, parser.meta_description)
    page = DiscoveredPage(url=url, title=" ".join(parser.title_parts).strip() or None,
        text=" ".join(text_parts)[:100_000], source_type="website",
        status_code=status,
        metadata={
            "content_type": "text/html",
            "signals": parser.signals,
            "meta_description": parser.meta_description,
        })
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
