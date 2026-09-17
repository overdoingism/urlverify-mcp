"""DNS-over-HTTPS cross-check. The system resolver (router, ISP, hosts file, local malware) can be poisoned; a DoH
resolver reached over TLS cannot be, short of a rogue CA — which the CT check covers. IPs are not compared literally
(GeoDNS legitimately differs): when the answer sets do not overlap, a TLS handshake is attempted against a DoH address;
system-path failure with DoH-path success is the signature of local DNS spoofing."""
from __future__ import annotations

from typing import Any

import httpx

from ..health import observe


async def resolve_doh(host: str, resolvers: list[str], timeout: float, user_agent: str) -> dict[str, Any]:
    out: dict[str, Any] = {"addresses": [], "resolvers": [], "dnssec_ad": None, "errors": []}
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent, "Accept": "application/dns-json"}) as c:
        for base in resolvers:
            got = False
            for rtype in ("A", "AAAA"):
                try:
                    r = await c.get(base, params={"name": host, "type": rtype})
                    r.raise_for_status()
                    j = r.json()
                    if j.get("AD") is not None and out["dnssec_ad"] is None:
                        out["dnssec_ad"] = bool(j.get("AD"))
                    for ans in j.get("Answer") or []:
                        if ans.get("type") in (1, 28) and ans.get("data"):
                            out["addresses"].append(ans["data"])
                            got = True
                except Exception as e:  # noqa: BLE001
                    out["errors"].append(f"{base}: {type(e).__name__}: {e}")
            observe("doh", got or not out["errors"], out["errors"][-1] if out["errors"] else None)
            if got:
                out["resolvers"].append(base)
    out["addresses"] = sorted(set(out["addresses"]))
    return out


def assess(system_ips: list[str], doh_ips: list[str], system_tls_ok: bool | None, doh_tls_ok: bool | None) -> tuple[str, bool, str]:
    """-> (status, fatal, message)."""
    if not doh_ips:
        return "skip", False, "DoH resolvers gave no answer; cross-check skipped"
    if set(system_ips) & set(doh_ips):
        return "pass", False, "system resolver and DoH agree"
    if system_tls_ok is False and doh_tls_ok:
        return "fail", True, ("system resolver answers differ from DoH and do not present a valid certificate for the host, "
                              "while the DoH-resolved address does: local DNS spoofing suspected")
    if system_tls_ok and doh_tls_ok:
        return "warn", False, "system resolver and DoH answers differ but both present a valid certificate (GeoDNS / CDN)"
    if system_tls_ok is None:
        return "skip", False, "answers differ; TLS comparison unavailable"
    return "warn", False, "system resolver and DoH answers differ; DoH path could not be validated either"
