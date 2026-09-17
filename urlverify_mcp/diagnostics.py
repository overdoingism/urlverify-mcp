"""Manual, lightweight endpoint probes (`urlverify-mcp check-env`, admin Test tab button). Never run automatically:
a probe is a snapshot, the observed health table (health.py) is the truth for a long-running service. Each probe is
the smallest request the service offers, so running it is polite."""
from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Config


async def probe_all(cfg: Config) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    ua = {"User-Agent": cfg.net.user_agent}

    async def run(name: str, target: str, coro):
        t0 = time.time()
        try:
            detail = await coro
            out.append({"name": name, "target": target, "ok": True, "detail": detail, "ms": int((time.time() - t0) * 1000)})
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "target": target, "ok": False, "detail": f"{type(e).__name__}: {e}", "ms": int((time.time() - t0) * 1000)})

    async with httpx.AsyncClient(timeout=10, headers=ua, follow_redirects=True) as c:
        async def llm():
            r = await c.get(cfg.llm.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {cfg.llm.api_key}"})
            r.raise_for_status()
            ids = [m.get("id") for m in r.json().get("data", [])][:5]
            return f"models: {ids}"

        async def searxng():
            if cfg.search.provider == "none":
                return "disabled (structured APIs only)"
            if cfg.search.provider == "mcp":
                r = await c.post(cfg.search.mcp.url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                          "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "urlverify-probe", "version": "0"}}},
                                 headers={"Accept": "application/json, text/event-stream"})
                r.raise_for_status()
                return "MCP initialize OK"
            r = await c.get(cfg.search.searxng_http.base_url.rstrip("/") + "/healthz")   # no search is issued
            if r.status_code == 404:
                r = await c.get(cfg.search.searxng_http.base_url.rstrip("/") + "/config")
            r.raise_for_status()
            return f"healthz {r.status_code}"

        async def fetch():
            r = await c.get("https://example.com/")
            r.raise_for_status()
            return "example.com reachable" if "Example Domain" in r.text else "unexpected body"

        async def wikipedia():
            r = await c.get("https://en.wikipedia.org/w/api.php", params={"action": "query", "meta": "siteinfo", "siprop": "general", "format": "json"})
            r.raise_for_status()
            return r.json()["query"]["general"].get("sitename", "ok")

        async def wayback():
            r = await c.get("https://archive.org/wayback/available", params={"url": "example.com"})
            r.raise_for_status()
            return "available API OK"

        async def github():
            r = await c.get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {cfg.identity.github_token}"} if cfg.identity.github_token else {})
            r.raise_for_status()
            core = r.json().get("resources", {}).get("core", {})
            return f"rate limit {core.get('remaining')}/{core.get('limit')} (not consumed by this probe)"

        async def pypi():
            r = await c.head("https://pypi.org/simple/pip/")
            r.raise_for_status()
            return f"simple index {r.status_code}"

        async def npm():
            r = await c.get("https://registry.npmjs.org/-/ping")
            r.raise_for_status()
            return "ping OK"

        await run("llm", cfg.llm.base_url, llm())
        await run("search", cfg.search.provider, searxng())
        await run("fetch", cfg.fetch.provider, fetch())
        await run("wikipedia", "siteinfo", wikipedia())
        await run("wayback", "availability API", wayback())
        await run("github", "/rate_limit", github())
        await run("pypi", "HEAD /simple/pip/", pypi())
        await run("npm", "/-/ping", npm())
    return out
