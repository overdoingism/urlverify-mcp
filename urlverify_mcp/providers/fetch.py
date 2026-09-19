"""Page fetchers. Default: built-in httpx fetch with a dependency-free HTML -> text converter that keeps headings,
lists and link targets (the agent needs to see where a page links to). Alternative: the fetch tool of the SearXNG MCP
server. Never downloads binaries; bodies are capped."""
from __future__ import annotations

import html as html_mod
import re
from typing import Protocol
from urllib.parse import urljoin

import httpx

from ..providers.public_http import public_client

from ..config import Config
from ..health import observe
from ..tracelog import TRACE

MAX_FETCH_BYTES = 2_000_000
ACCEPT = "text/html,application/xhtml+xml,application/json;q=0.9,text/plain;q=0.8,*/*;q=0.1"


class Fetcher(Protocol):
    async def fetch(self, url: str) -> str: ...
    async def close(self) -> None: ...


def html_to_text(doc: str, base_url: str = "") -> str:
    """Readable text: drops script/style/nav noise, keeps headings (#), list bullets, and links as 'text (url)'."""
    s = re.sub(r"<!--.*?-->", " ", doc, flags=re.S)
    s = re.sub(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    title = re.search(r"<title[^>]*>(.*?)</title>", s, flags=re.S | re.I)
    # links: keep the target, resolved against the page
    def _link(m):
        href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if not text:
            return " "
        target = urljoin(base_url, html_mod.unescape(href)) if base_url else html_mod.unescape(href)
        if target.startswith(("javascript:", "#")):
            return text
        return f"{text} ({target})"
    s = re.sub(r"<a\s[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", _link, s, flags=re.S | re.I)
    s = re.sub(r"<h([1-6])[^>]*>(.*?)</h\1>", lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2) + "\n", s, flags=re.S | re.I)
    s = re.sub(r"<li[^>]*>", "\n- ", s, flags=re.I)
    s = re.sub(r"<(br|/p|/div|/tr|/table|/ul|/ol|/section|/article|/header|/footer|/blockquote|/pre)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<(td|th)[^>]*>", " | ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if title:
        t = html_mod.unescape(re.sub(r"\s+", " ", title.group(1))).strip()
        if t:
            s = f"title: {t}\n\n{s}"
    return s


class BuiltinFetcher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sources: dict[str, str] = {}
        self.client = public_client(timeout=cfg.search.call_timeout_s, follow_redirects=True,
                                        headers={"User-Agent": cfg.net.user_agent, "Accept": ACCEPT, "Accept-Language": "en,*;q=0.5"})

    async def fetch(self, url: str) -> str:
        TRACE.log("fetch_request", provider="builtin", url=url)
        try:
            return await self._fetch(url)
        except httpx.HTTPError as e:
            observe("fetch:builtin", False, f"{type(e).__name__}: {e}")   # transport-level only; a 4xx page is the site's answer, not our failure
            raise

    async def _fetch(self, url: str) -> str:
        async with self.client.stream("GET", url) as r:
            self.sources[url] = str(r.url)
            ct = r.headers.get("content-type", "")
            if not any(t in ct for t in ("text", "json", "xml", "javascript")):
                text = f"(binary content-type {ct}, {r.headers.get('content-length')} bytes; body not downloaded)"
                TRACE.log("fetch_response", provider="builtin", url=url, status=r.status_code, content_type=ct, chars=len(text), text=text)
                return text
            chunks, size = [], 0
            async for chunk in r.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FETCH_BYTES:
                    break
            body = b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
            final_url = str(r.url)
            status = r.status_code
        text = html_to_text(body, final_url) if "html" in ct else body
        observe("fetch:builtin", True)
        if status >= 400:
            text = f"(HTTP {status})\n" + text
        TRACE.log("fetch_response", provider="builtin", url=url, final_url=final_url, status=status, content_type=ct, chars=len(text), text=text)
        return text

    async def close(self) -> None:
        await self.client.aclose()


class MCPFetcher:
    """Delegates to the SearXNG MCP server's fetch tool (HTML -> markdown, PDF text)."""

    def __init__(self, mcp_provider, cfg: Config, owns_session: bool = False):
        self.mcp = mcp_provider
        self.cfg = cfg
        self.sources: dict[str, str] = {}
        self.owns_session = owns_session

    async def fetch(self, url: str) -> str:
        from ..checks.redirects import expand
        from .public_http import UnsafeURL
        result = await expand(url, self.cfg.net.timeout_s, self.cfg.net.user_agent)
        if result.get("blocked"):
            raise UnsafeURL(result["error"])
        if result.get("error"):
            raise httpx.RequestError(result["error"])
        self.sources[url] = result.get("final_url") or url
        # The remote fetch service must enforce its own network egress restrictions too.
        return await self.mcp.fetch(url)

    async def close(self) -> None:
        if self.owns_session:
            await self.mcp.close()


def make_fetcher(cfg: Config, search_provider) -> Fetcher:
    if cfg.fetch.provider == "mcp":
        from .search import MCPSearchProvider
        if isinstance(search_provider, MCPSearchProvider):
            return MCPFetcher(search_provider, cfg)
        return MCPFetcher(MCPSearchProvider(cfg), cfg, owns_session=True)   # separate session when search is not the MCP provider
    return BuiltinFetcher(cfg)
