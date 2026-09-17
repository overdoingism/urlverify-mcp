"""Search / fetch providers. Default: the existing SearXNG MCP server (Streamable HTTP) as an MCP client.
Fallback: SearXNG JSON API + direct httpx fetch."""
from __future__ import annotations

import html
import re
import time
from typing import Any, Protocol

import httpx

from ..config import Config
from ..tracelog import TRACE


MAX_FETCH_BYTES = 2_000_000   # hard cap for any page body we pull ourselves


class SearchUnavailable(RuntimeError):
    """The configured search backend cannot be reached. Ordinary Exception, so callers can degrade gracefully."""


def _root_cause(e: BaseException) -> str:
    seen = 0
    while seen < 10:
        seen += 1
        subs = getattr(e, "exceptions", None)
        if subs:
            e = subs[0]
            continue
        if e.__cause__ is not None:
            e = e.__cause__
            continue
        break
    if isinstance(e, BaseException) and type(e).__name__ == "CancelledError":
        return "connection failed (cancelled by transport)"
    return f"{type(e).__name__}: {e}"


class SearchProvider(Protocol):
    async def search(self, query: str) -> str: ...
    async def fetch(self, url: str) -> str: ...
    async def close(self) -> None: ...


def _strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


class MCPSearchProvider:
    """Talks to a Streamable-HTTP MCP server exposing search + fetch tools. One session per verification.

    The transport's context managers live in a dedicated worker task: when the endpoint is dead, anyio cancels
    *that* task's scope, and the caller receives an ordinary SearchUnavailable through a Future instead of a
    CancelledError that would escape every `except Exception`.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._worker: "asyncio.Task | None" = None
        self._ready: "asyncio.Future | None" = None
        self._stop: "asyncio.Event | None" = None
        self._session = None
        self._dead_reason: str | None = None

    async def _run_worker(self) -> None:
        import asyncio
        from mcp import ClientSession
        try:
            from mcp.client.streamable_http import streamable_http_client as streamablehttp_client  # mcp >= 1.30
        except ImportError:  # older 1.x
            from mcp.client.streamable_http import streamablehttp_client

        url = self.cfg.search.mcp.url
        try:
            async with streamablehttp_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set_result(session)
                    await self._stop.wait()
        except BaseException as e:  # noqa: BLE001  (CancelledError from the transport's cancel scope lands here)
            reason = f"search MCP at {url} unreachable: {_root_cause(e)}"
            self._dead_reason = reason
            if not self._ready.done():
                self._ready.set_exception(SearchUnavailable(reason))
            elif isinstance(e, asyncio.CancelledError) and not self._stop.is_set():
                raise  # genuine external cancellation after we were already connected
        finally:
            self._session = None

    async def _ensure(self):
        import asyncio
        if self._session is not None:
            return
        if self._dead_reason:
            raise SearchUnavailable(self._dead_reason)
        if self._worker is None:
            loop = asyncio.get_running_loop()
            self._ready = loop.create_future()
            self._stop = asyncio.Event()
            self._worker = asyncio.create_task(self._run_worker(), name="urlverify-mcp-search")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), timeout=max(self.cfg.net.timeout_s, 10))
        except asyncio.TimeoutError:
            self._dead_reason = f"search MCP at {self.cfg.search.mcp.url} did not answer initialize within {max(self.cfg.net.timeout_s, 10)}s"
            await self.close()
            raise SearchUnavailable(self._dead_reason) from None

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        await self._ensure()
        TRACE.log("search_request", provider="mcp", url=self.cfg.search.mcp.url, tool=tool, args=args)
        t0 = time.time()
        try:
            from datetime import timedelta
            result = await self._session.call_tool(tool, args, read_timeout_seconds=timedelta(seconds=self.cfg.search.call_timeout_s))
        except Exception as e:  # transport died mid-call, or read timeout (McpError -32001)
            TRACE.log("search_response", provider="mcp", tool=tool, error=_root_cause(e), elapsed_s=round(time.time() - t0, 2))
            raise SearchUnavailable(f"search MCP call {tool} failed: {_root_cause(e)}") from None
        parts = []
        for c in result.content:
            t = getattr(c, "text", None)
            if t:
                parts.append(t)
        text = "\n".join(parts)
        TRACE.log("search_response", provider="mcp", tool=tool, is_error=bool(getattr(result, "isError", False)),
                  elapsed_s=round(time.time() - t0, 2), chars=len(text), text=text)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {tool} error: {text[:300]}")
        return text

    async def search(self, query: str) -> str:
        return await self._call(self.cfg.search.mcp.search_tool, {"query": query})

    async def fetch(self, url: str) -> str:
        return await self._call(self.cfg.search.mcp.fetch_tool, {"url": url})

    async def close(self) -> None:
        import asyncio
        if self._worker is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._worker, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._worker.cancel()
            try:
                await self._worker
            except BaseException:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass
        self._worker = None
        self._session = None


class SearxngHTTPProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = httpx.AsyncClient(timeout=cfg.search.call_timeout_s, headers={"User-Agent": cfg.net.user_agent}, follow_redirects=True)

    async def search(self, query: str) -> str:
        TRACE.log("search_request", provider="searxng_http", url=self.cfg.search.searxng_http.base_url, tool="search", args={"query": query})
        try:
            r = await self.client.get(self.cfg.search.searxng_http.base_url.rstrip("/") + "/search",
                                      params={"q": query, "format": "json"})
            r.raise_for_status()
        except httpx.HTTPError as e:
            TRACE.log("search_response", provider="searxng_http", tool="search", error=f"{type(e).__name__}: {e}")
            raise SearchUnavailable(f"SearXNG at {self.cfg.search.searxng_http.base_url} unreachable: {type(e).__name__}: {e}") from None
        data = r.json()
        lines = []
        for i, item in enumerate(data.get("results", [])[:10], 1):
            lines.append(f"{i}. {item.get('title','')}\n   URL: {item.get('url','')}\n   {(item.get('content') or '')[:300]}")
        text = "\n".join(lines) or "(no results)"
        TRACE.log("search_response", provider="searxng_http", tool="search", chars=len(text), text=text, raw_results=data.get("results", [])[:10])
        return text

    async def fetch(self, url: str) -> str:
        from .fetch import BuiltinFetcher
        f = BuiltinFetcher(self.cfg)
        try:
            return await f.fetch(url)
        finally:
            await f.close()

    async def close(self) -> None:
        await self.client.aclose()


class NoSearchProvider:
    """search.provider: none — every search reports 'unavailable'; the agent relies on structured APIs."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def search(self, query: str) -> str:
        raise SearchUnavailable("web search is disabled (search.provider: none); use structured lookups")

    async def fetch(self, url: str) -> str:
        raise SearchUnavailable("no search provider configured")

    async def close(self) -> None:
        pass


def make_search_provider(cfg: Config) -> SearchProvider:
    if cfg.search.provider == "mcp":
        return MCPSearchProvider(cfg)
    if cfg.search.provider == "none":
        return NoSearchProvider(cfg)
    return SearxngHTTPProvider(cfg)
