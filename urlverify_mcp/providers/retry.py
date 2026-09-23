"""Polite retry for third-party GETs: only for "try again later" answers (429, 502, 503, 504), with backoff and the
server's Retry-After honoured up to a cap. Other statuses and every network error are returned / raised at once."""
from __future__ import annotations

import asyncio

import httpx

RETRY_STATUSES = {429, 502, 503, 504}
MAX_RETRY_AFTER_S = 10.0


async def get_with_retry(client: httpx.AsyncClient, url: str, retries: int = 2, backoff_s: float = 3.0, **kw) -> httpx.Response:
    attempt = 0
    while True:
        r = await client.get(url, **kw)
        if r.status_code not in RETRY_STATUSES or attempt >= retries:
            return r
        wait = backoff_s * (2 ** attempt)
        ra = r.headers.get("retry-after", "")
        if ra.isdigit():
            wait = max(wait, float(ra))
        await asyncio.sleep(min(wait, MAX_RETRY_AFTER_S))
        attempt += 1
