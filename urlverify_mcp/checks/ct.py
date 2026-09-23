"""Certificate Transparency first-seen via crt.sh (best effort; failures are non-fatal)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..health import observe


async def first_seen(etld1: str, timeout: float, user_agent: str, store=None, cache_days: int = 90,
                     retries: int = 2, backoff_s: float = 3.0) -> dict[str, Any]:
    """Oldest certificate for the domain in CT. The date is historical and never changes, so a found date is cached
    (state/kv.json) and crt.sh -- an overloaded volunteer service -- is asked again only after `cache_days`."""
    import json
    import time
    key = f"ct_first_seen:{etld1}"
    if store is not None and cache_days > 0:
        try:
            c = json.loads(store.kv_get(key) or "null")
        except ValueError:
            c = None
        if c and time.time() - c.get("fetched_at", 0) < (cache_days if c.get("first_seen") else 1) * 86400:
            out = {k: v for k, v in c.items() if k != "fetched_at"}
            if out.get("first_seen"):
                out["age_days"] = (datetime.now(timezone.utc) - datetime.fromisoformat(out["first_seen"])).days
            out["cached"] = True
            return out
    res = await _first_seen_live(etld1, timeout, user_agent, retries, backoff_s)
    if store is not None and res.get("ok"):
        try:
            store.kv_set(key, json.dumps({**{k: v for k, v in res.items() if k != "age_days"}, "fetched_at": time.time()}))
        except Exception:  # noqa: BLE001
            pass
    return res


async def _first_seen_live(etld1: str, timeout: float, user_agent: str, retries: int, backoff_s: float) -> dict[str, Any]:
    from ..providers.retry import get_with_retry
    url = f"https://crt.sh/?q={etld1}&output=json"
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}) as client:
            r = await get_with_retry(client, url, retries, backoff_s)
            if r.status_code != 200:
                observe("crt.sh", False, f"status {r.status_code}")
                return {"ok": False, "error": f"crt.sh status {r.status_code}"}
            data = r.json()
            observe("crt.sh", True)
    except Exception as e:  # noqa: BLE001
        observe("crt.sh", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    dates = []
    for row in data if isinstance(data, list) else []:
        nb = row.get("not_before")
        if nb:
            try:
                dates.append(datetime.fromisoformat(nb.replace("Z", "")).replace(tzinfo=timezone.utc))
            except ValueError:
                pass
    if not dates:
        return {"ok": True, "count": 0, "first_seen": None}
    first = min(dates)
    age_days = (datetime.now(timezone.utc) - first).days
    return {"ok": True, "count": len(dates), "first_seen": first.isoformat(), "age_days": age_days}


def parse_crtsh_cert_page(status: int, content_type: str, body: str) -> dict[str, Any]:
    """crt.sh has no JSON for fingerprint queries; the HTML page is unambiguous: 'Certificate not found' or a cert page."""
    if status != 200:
        return {"ok": False, "error": f"crt.sh HTTP {status}"}
    if "text/html" not in content_type:
        return {"ok": False, "error": f"crt.sh unexpected content-type {content_type!r}"}
    low = body.lower()
    if "certificate not found" in low:
        return {"ok": True, "logged": False}
    if "crt.sh id" in low or "certificate fingerprint" in low or "sha-256" in low:
        return {"ok": True, "logged": True}
    if "unsupported output type" in low:
        return {"ok": False, "error": "crt.sh rejected the query format"}
    if "<title>crt.sh</title>" in low and "error" in low:
        return {"ok": False, "error": "crt.sh error page"}
    return {"ok": False, "error": "crt.sh page not recognised"}


async def cert_logged(fingerprint_sha256: str, timeout: float, user_agent: str) -> dict[str, Any]:
    """Is this exact leaf certificate known to Certificate Transparency (via crt.sh)? Best effort; explicit errors."""
    url = f"https://crt.sh/?sha256={fingerprint_sha256}"
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}) as client:
            r = await client.get(url)
    except Exception as e:  # noqa: BLE001
        observe("crt.sh", False, f"{type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "source": url}
    parsed = parse_crtsh_cert_page(r.status_code, r.headers.get("content-type", ""), r.text[:200_000])
    observe("crt.sh", parsed.get("ok", False), parsed.get("error"))
    parsed["source"] = url
    return parsed
