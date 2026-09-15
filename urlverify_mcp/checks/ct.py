"""Certificate Transparency first-seen via crt.sh (best effort; failures are non-fatal)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx


async def first_seen(etld1: str, timeout: float, user_agent: str) -> dict[str, Any]:
    url = f"https://crt.sh/?q={etld1}&output=json"
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return {"ok": False, "error": f"crt.sh status {r.status_code}"}
            data = r.json()
    except Exception as e:  # noqa: BLE001
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
