"""L0 orchestrator: deterministic checks that never involve the LLM."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from ..cache.anchors import all_anchor_domains, anchor_for, extract_path_identity
from ..config import Config
from ..models import CheckResult, L0Result
from ..storage import Storage
from . import ct as ct_mod
from . import dns as dns_mod
from . import redirects as redir_mod
from . import tls as tls_mod
from .injection import find_injection
from .private import looks_local_hostname, non_public
from .urltools import analyse_host, etld1_of, host_of, normalize_url


async def run_l0(url: str, cfg: Config, store: Storage, known_official: list[str] | None = None,
                 cache_hits: list[str] | None = None) -> L0Result:
    cache_hits = cache_hits if cache_hits is not None else []
    norm = normalize_url(url)
    host = host_of(norm)
    e1 = etld1_of(host)
    res = L0Result(normalized_url=norm, host=host, etld1=e1)
    scheme = urlsplit(norm).scheme

    # --- denylist
    deny = {d.lower() for d in cfg.lists.denylist}
    if host in deny or e1 in deny:
        res.checks.append(CheckResult(name="denylist", status="fail", fatal=True, message=f"{host} is on the denylist"))
    else:
        res.checks.append(CheckResult(name="denylist", status="pass"))

    # --- platform anchor
    anchor = anchor_for(e1)
    if anchor:
        res.platform = anchor.platform
        owner, repo = extract_path_identity(anchor, urlsplit(norm).path)
        res.platform_owner, res.platform_repo = owner, repo
        cache_hits.append("platform_anchor")
        res.checks.append(CheckResult(name="platform_anchor", status="pass",
                                      detail={"platform": anchor.platform, "owner": owner, "repo": repo},
                                      message=f"{e1} is a known hosting platform; path owner '{owner}' still requires identity verification"))

    # --- scheme
    if scheme != "https":
        res.checks.append(CheckResult(name="scheme", status="fail", fatal=True, message=f"scheme is {scheme}, not https"))
    else:
        res.checks.append(CheckResult(name="scheme", status="pass"))

    # --- host structure (homoglyph / typosquat / subdomain abuse / shortener)
    known = list(all_anchor_domains()) + [d.lower() for d in (known_official or [])]
    hf = analyse_host(host, known)
    struct_msgs = []
    fatal_struct = False
    if hf["ip_literal"]:
        struct_msgs.append("IP-literal host"); fatal_struct = True
    if hf["punycode"] or hf["mixed_script"]:
        struct_msgs.append("punycode / mixed-script hostname"); res.risk_signals.append("idn_host")
    if hf["confusable_of"]:
        struct_msgs.append(f"confusable look-alike of {hf['confusable_of']}"); fatal_struct = True
    if hf["subdomain_abuse_of"]:
        struct_msgs.append(f"embeds official domain {hf['subdomain_abuse_of']} as a subdomain label"); fatal_struct = True
    if hf["typosquat_of"]:
        struct_msgs.append(f"possible typosquat of {hf['typosquat_of']}"); res.risk_signals.append(f"typosquat:{hf['typosquat_of']}")
    if hf.get("brand_in_label_of"):
        struct_msgs.append(f"brand name of {hf['brand_in_label_of']} embedded in a different registrable domain"); res.risk_signals.append(f"brand_in_label:{hf['brand_in_label_of']}")
    if hf["shortener"]:
        struct_msgs.append("URL shortener"); res.risk_signals.append("shortener")
    res.checks.append(CheckResult(name="host_structure", status="fail" if fatal_struct else ("warn" if struct_msgs else "pass"),
                                  fatal=fatal_struct, detail=hf, message="; ".join(struct_msgs)))

    # --- address class first: never probe loopback / private / link-local targets
    dns_r = await dns_mod.resolve(host)
    bad_ips = non_public(dns_r.get("addresses", [])) if dns_r.get("resolved") else []
    if looks_local_hostname(host) or bad_ips or hf["ip_literal"] and non_public([host.strip("[]")]):
        res.checks.append(CheckResult(name="public_address", status="fail", fatal=True,
                                      detail={"addresses": dns_r.get("addresses", []), "non_public": bad_ips},
                                      message="non-public address (loopback / private / link-local); nothing to verify here"))
        res.checks.append(CheckResult(name="dns", status="pass" if dns_r.get("resolved") else "fail", fatal=not dns_r.get("resolved"), detail=dns_r))
        for name in ("tls", "redirects", "ct_first_seen"):
            res.checks.append(CheckResult(name=name, status="skip", message="skipped: non-public target"))
        return res
    res.checks.append(CheckResult(name="public_address", status="pass", detail={"addresses": dns_r.get("addresses", [])}))

    # --- TLS (cached), redirects, CT in parallel
    dns_task = asyncio.sleep(0, result=dns_r)
    cert_cached = store.get_cert(host)
    if cert_cached:
        cache_hits.append("cert")
        tls_task = asyncio.sleep(0, result=cert_cached)
    else:
        tls_task = tls_mod.fetch_cert(host, urlsplit(norm).port or 443, cfg.net.timeout_s)
    redir_task = redir_mod.expand(norm, cfg.net.timeout_s, cfg.net.user_agent)
    ct_task = asyncio.sleep(0, result={"ok": False, "error": "skipped for platform anchor"}) if anchor else ct_mod.first_seen(e1, cfg.net.timeout_s, cfg.net.user_agent)
    dns_r, tls_r, redir_r, ct_r = await asyncio.gather(dns_task, tls_task, redir_task, ct_task, return_exceptions=True)

    # DNS
    if isinstance(dns_r, Exception) or not dns_r.get("resolved"):
        res.checks.append(CheckResult(name="dns", status="fail", fatal=True, message="host does not resolve",
                                      detail=dns_r if isinstance(dns_r, dict) else {"error": str(dns_r)}))
    else:
        res.checks.append(CheckResult(name="dns", status="pass", detail=dns_r))

    # TLS
    if isinstance(tls_r, Exception):
        res.checks.append(CheckResult(name="tls", status="error", fatal=False, message=str(tls_r)))
    else:
        if tls_r.get("trusted"):
            if not cert_cached:
                store.put_cert(host, tls_r, cfg.cache.cert_ttl_hours * 3600)
            res.tls_org = tls_r.get("subject_org")
            detail = {k: tls_r.get(k) for k in ("issuer", "issuer_org", "subject_org", "validation_level", "not_after_ts", "fingerprint_sha256", "trust_store")}
            msg = f"trusted chain ({tls_r.get('validation_level')}, issuer {tls_r.get('issuer_org') or tls_r.get('issuer')})"
            if anchor and anchor.expected_issuers and tls_r.get("issuer_org") and not any(x.lower() in (tls_r.get("issuer_org") or "").lower() for x in anchor.expected_issuers):
                res.risk_signals.append("issuer_drift")
                msg += f"; issuer differs from expected for {anchor.platform}"
            res.checks.append(CheckResult(name="tls", status="pass", detail=detail, message=msg))
        else:
            err = tls_r.get("error") or "unknown"
            transient = err.startswith("timeout") or err.startswith("connection error")
            res.checks.append(CheckResult(name="tls", status="error" if transient else "fail", fatal=not transient,
                                          detail={k: tls_r.get(k) for k in ("error", "issuer", "subject_org", "self_signed", "expired", "hostname_match")},
                                          message=err))

    # Redirects
    if isinstance(redir_r, Exception):
        res.checks.append(CheckResult(name="redirects", status="error", message=str(redir_r)))
    else:
        res.final_url = redir_r.get("final_url")
        res.final_etld1 = etld1_of(host_of(res.final_url)) if res.final_url else None
        detail = {k: redir_r.get(k) for k in ("chain", "final_url", "final_status", "content_type", "content_length", "hops", "error")}
        if redir_r.get("error") and not redir_r.get("chain"):
            res.checks.append(CheckResult(name="redirects", status="error", detail=detail, message=redir_r["error"]))
        elif res.final_url and host_of(res.final_url) != host and (looks_local_hostname(host_of(res.final_url)) or
                                                                    non_public((await dns_mod.resolve(host_of(res.final_url))).get("addresses", []))):
            res.checks.append(CheckResult(name="redirects", status="fail", fatal=True, detail=detail,
                                          message=f"redirects to a non-public address: {host_of(res.final_url)}"))
        elif res.final_etld1 and res.final_etld1 != e1:
            fa = anchor_for(res.final_etld1)
            same_platform = anchor and fa and fa.platform == anchor.platform
            status = "warn" if same_platform else "warn"
            res.risk_signals.append(f"cross_domain_redirect:{res.final_etld1}")
            res.checks.append(CheckResult(name="redirects", status=status, detail=detail,
                                          message=f"redirects to a different registrable domain: {res.final_etld1}"))
        else:
            res.checks.append(CheckResult(name="redirects", status="pass", detail=detail,
                                          message=f"{redir_r.get('hops', 0)} hop(s), final status {redir_r.get('final_status')}"))

    # CT
    if isinstance(ct_r, Exception) or not ct_r.get("ok"):
        res.checks.append(CheckResult(name="ct_first_seen", status="skip", message=(ct_r.get("error") if isinstance(ct_r, dict) else str(ct_r))))
    else:
        age = ct_r.get("age_days")
        if age is not None and age < cfg.identity.new_domain_days and not anchor:
            res.risk_signals.append(f"new_domain:{age}d")
            res.checks.append(CheckResult(name="ct_first_seen", status="warn", detail=ct_r, message=f"first certificate seen only {age} days ago"))
        else:
            res.checks.append(CheckResult(name="ct_first_seen", status="pass", detail=ct_r,
                                          message=f"first certificate seen {age} days ago" if age is not None else "no CT records"))
    return res


def apply_injection_check(res: L0Result, page_text: str | None, patterns: list[str]) -> None:
    """Called once the target page text has been fetched (by the agent's fetch tool)."""
    if page_text is None:
        res.checks.append(CheckResult(name="injection", status="skip", message="target page not fetched"))
        return
    hits = find_injection(page_text, patterns)
    if hits:
        res.checks.append(CheckResult(name="injection", status="fail", fatal=True, detail={"hits": hits},
                                      message="page contains text addressed to AI agents / verifiers: " + hits[0]["match"]))
    else:
        res.checks.append(CheckResult(name="injection", status="pass"))
