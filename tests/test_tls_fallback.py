"""TLS check tries resolved addresses in turn (IPv6 without a route must not fail a dual-stack host), but a TLS answer
from any address is final: a bad certificate is never hidden by trying another address."""
from urlverify_mcp.checks import tls


async def test_unreachable_family_falls_through(monkeypatch):
    calls = []

    async def fake(host, port, timeout, ip):
        calls.append(ip)
        if ":" in ip:
            return {"trusted": False, "error": "connection error: [Errno 101] Network is unreachable"}
        return {"trusted": True, "error": None}
    monkeypatch.setattr(tls, "fetch_cert", fake)
    r = await tls.fetch_cert_any("h", 443, 5, ["2600::1", "2600::2", "65.9.180.1"])
    assert r["trusted"] and calls[:2] == ["2600::1", "65.9.180.1"]              # families interleaved
    assert r["unreachable_addresses"][0].startswith("2600::1")


async def test_bad_certificate_is_final(monkeypatch):
    calls = []

    async def fake(host, port, timeout, ip):
        calls.append(ip)
        return {"trusted": False, "error": "cert verification failed: self-signed"} if ip == "1.1.1.1" else {"trusted": True}
    monkeypatch.setattr(tls, "fetch_cert", fake)
    r = await tls.fetch_cert_any("h", 443, 5, ["1.1.1.1", "2.2.2.2"])
    assert not r["trusted"] and calls == ["1.1.1.1"]


async def test_all_unreachable(monkeypatch):
    async def fake(host, port, timeout, ip):
        return {"trusted": False, "error": "timeout: timed out"}
    monkeypatch.setattr(tls, "fetch_cert", fake)
    r = await tls.fetch_cert_any("h", 443, 5, ["1.1.1.1", "2.2.2.2"])
    assert r["error"].startswith("no resolved address accepted a connection")
