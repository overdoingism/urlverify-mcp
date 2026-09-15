"""Expand the redirect chain without downloading bodies. HEAD first, GET (streamed, closed early) fallback."""
from __future__ import annotations

from typing import Any

import httpx

MAX_HOPS = 10


async def expand(url: str, timeout: float, user_agent: str) -> dict[str, Any]:
    chain: list[dict[str, Any]] = []
    current = url
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    content_type = None
    content_length = None
    status = None
    error = None
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers) as client:
        for _ in range(MAX_HOPS):
            try:
                resp = await client.head(current)
                if resp.status_code in (405, 403, 400, 501) or resp.status_code >= 500:
                    # some servers reject HEAD; stream a GET and close immediately
                    async with client.stream("GET", current) as r2:
                        resp = r2
                        status = r2.status_code
                        hdrs = r2.headers
                        loc = hdrs.get("location")
                        content_type = hdrs.get("content-type")
                        content_length = hdrs.get("content-length")
                else:
                    status = resp.status_code
                    hdrs = resp.headers
                    loc = hdrs.get("location")
                    content_type = hdrs.get("content-type")
                    content_length = hdrs.get("content-length")
            except httpx.HTTPError as e:
                error = f"{type(e).__name__}: {e}"
                break
            chain.append({"url": current, "status": status})
            if status in (301, 302, 303, 307, 308) and loc:
                nxt = str(httpx.URL(current).join(loc))
                if nxt == current:
                    break
                current = nxt
                continue
            break
    return {"chain": chain, "final_url": current, "final_status": status, "content_type": content_type,
            "content_length": content_length, "error": error, "hops": max(0, len(chain) - 1)}
