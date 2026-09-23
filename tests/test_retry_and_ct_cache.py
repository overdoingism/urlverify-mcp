import json
import time

import httpx

from urlverify_mcp.checks import ct
from urlverify_mcp.providers.retry import get_with_retry


async def test_retry_only_on_try_again_statuses(monkeypatch):
    seq = iter([502, 503, 200])
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(next(seq), text="[]")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await get_with_retry(c, "https://x.example/", retries=2, backoff_s=0.01)
    assert r.status_code == 200 and len(calls) == 3
    calls.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: (calls.append(1), httpx.Response(404))[1])) as c:
        r = await get_with_retry(c, "https://x.example/", retries=2, backoff_s=0.01)
    assert r.status_code == 404 and len(calls) == 1


class KV:
    def __init__(self):
        self.d = {}

    def kv_get(self, k):
        return self.d.get(k)

    def kv_set(self, k, v):
        self.d[k] = v


async def test_ct_first_seen_is_cached(monkeypatch):
    live = []

    async def fake_live(etld1, *a):
        live.append(etld1)
        return {"ok": True, "count": 3, "first_seen": "2015-01-01T00:00:00+00:00", "age_days": 1}
    monkeypatch.setattr(ct, "_first_seen_live", fake_live)
    kv = KV()
    a = await ct.first_seen("example.org", 5, "t", kv, 90)
    b = await ct.first_seen("example.org", 5, "t", kv, 90)
    assert live == ["example.org"] and b["cached"] and b["age_days"] > 3000 and a["first_seen"] == b["first_seen"]
    # a "no certificate" answer is only kept for a day
    kv.d["ct_first_seen:new.example"] = json.dumps({"ok": True, "count": 0, "first_seen": None, "fetched_at": time.time() - 2 * 86400})
    await ct.first_seen("new.example", 5, "t", kv, 90)
    assert live[-1] == "new.example"
