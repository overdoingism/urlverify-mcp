"""Certificate Transparency first-seen via crt.sh (best effort; failures are non-fatal)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..health import observe


async def first_seen(etld1: str, timeout: float, user_agent: str) -> dict[str, Any]:
    url = f"https://crt.sh/?q={etld1}&output=json"
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}) as client:
            r = await client.get(url)
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
