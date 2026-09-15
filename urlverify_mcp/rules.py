"""Verdict rules engine. The LLM proposes; these rules verify and decide. The LLM cannot override them."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .cache.anchors import anchor_for
from .checks.urltools import etld1_of, host_of
from .config import Config
from .identity.sources import classify
from .models import Evidence, L0Result, LLMSubmission, Verdict


@dataclass
class Decision:
    verdict: Verdict
    confidence: float
    notes: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    supporting_sources: dict[str, set[str]] = field(default_factory=dict)   # official domain -> distinct source eTLD+1s
    established_domains: list[str] = field(default_factory=list)
    established_orgs: dict[str, list[str]] = field(default_factory=dict)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


STRUCTURED_KINDS = {"wikidata", "wikipedia", "github", "huggingface", "wayback", "package_registry", "distro"}
_DOMAIN_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")


def _anchors(ev: Evidence) -> set[str]:
    """Fact anchors that must appear in the raw source output: domains and quoted names from claim + quote."""
    text = _norm_ws(ev.claim + " " + ev.quote)
    out = set(_DOMAIN_RE.findall(text))
    out |= {m.lower() for m in re.findall(r"'([^']{3,40})'", ev.claim + " " + ev.quote)}
    return out


def verify_quotes(evidence: list[Evidence], store: dict[str, str]) -> None:
    """Mark evidence.verified_quote. Page/media evidence needs a verbatim quote; structured (API) evidence is
    fact-anchored: the domain / org named in the claim must literally appear in that source's raw output."""
    store_norm = {k: _norm_ws(v) for k, v in store.items()}
    for ev in evidence:
        q = _norm_ws(ev.quote)
        src = ev.source
        if ev.kind in STRUCTURED_KINDS:
            h = host_of(src)
            texts = [store_norm[k] for k in store_norm if k == src or k.rstrip("/") == src.rstrip("/")]
            if not texts:
                texts = [store_norm[k] for k in store_norm if h and host_of(k) == h]
            if not texts:
                # api results are also stored under "<tool>:<args>" keys; match by kind name
                texts = [store_norm[k] for k in store_norm if k.startswith(ev.kind) or (ev.kind == "package_registry" and k.startswith("package_registry"))]
            anchors = _anchors(ev)
            if texts and (any(q in t for t in texts if len(q) >= 8) or (anchors and any(a in t for a in anchors for t in texts))):
                ev.verified_quote = True
                ev.notes.append("structured source: fact anchors found in raw tool output")
            else:
                ev.verified_quote = False
                ev.notes.append("structured source: claimed facts not found in the tool output for that source; discarded")
            continue
        if len(q) < 8:
            ev.verified_quote = False
            ev.notes.append("quote too short to verify")
            continue
        candidates = [store_norm[k] for k in store_norm if k == src or k.rstrip("/") == src.rstrip("/")]
        if not candidates:
            # same host fallback (e.g. api url vs page url)
            h = host_of(src)
            candidates = [store_norm[k] for k in store_norm if host_of(k) == h and h]
        if not candidates:
            candidates = list(store_norm.values())
            ev.notes.append("source not fetched under that exact URL; matched against all fetched content")
        ev.verified_quote = any(q in c for c in candidates)
        if not ev.verified_quote:
            ev.notes.append("quote not found in fetched content; evidence discarded")


def decide(cfg: Config, l0: L0Result, sub: LLMSubmission, store: dict[str, str], project: str,
           cached: dict | None = None, ages: dict[str, dict] | None = None,
           target_domain_age: dict | None = None) -> Decision:
    notes: list[str] = []
    ic = cfg.identity
    ages = ages or {}
    promoted = 0
    contradictions: list[str] = []

    # ---- 1. evidence hygiene
    evidence = list(sub.evidence)
    verify_quotes(evidence, store)
    self_domains = {l0.etld1}
    for d in sub.identity.official_domains:
        self_domains.add(etld1_of(d))
    usable: list[Evidence] = []
    for ev in evidence:
        tier, why = classify(ev.source, ev.tier, ic)
        ev.tier = tier
        ev.notes.append(why)
        src_e1 = etld1_of(host_of(ev.source))
        if not ev.verified_quote:
            continue
        if src_e1 in self_domains and ev.kind not in ("github", "huggingface", "wayback", "wikidata", "wikipedia", "package_registry"):
            ev.notes.append("self-attestation (source is the candidate/target domain); not counted")
            continue
        if tier == 3 and not ic.allow_tier3:
            age = ages.get(ev.source)
            ok, why_age = _aged_enough(age, ic, l0, target_domain_age, cfg)
            if ok and promoted < ic.tier3_aged_max_count:
                promoted += 1
                ev.tier = 2
                ev.notes.append(f"tier-3 source promoted to tier 2: {why_age}")
                notes.append(f"aged tier-3 source counted: {ev.source} ({why_age})")
            else:
                if ok:
                    ev.notes.append(f"aged tier-3 source not counted: tier3_aged_max_count={ic.tier3_aged_max_count} already used")
                else:
                    ev.notes.append(f"tier-3 source excluded: {why_age}")
                if why_age.startswith("predates"):
                    contradictions.append(f"{ev.source}: {why_age}")
                continue
        # temporal stability: wikipedia/wikidata evidence with a recent change is demoted
        if ev.kind in ("wikipedia", "wikidata"):
            raw = store.get(ev.source, "")
            if '"recent_change": true' in raw:
                ev.tier = max(ev.tier, 2)
                ev.notes.append("editable source changed within the history window; demoted to tier 2, cannot stand alone")
                notes.append(f"{ev.kind} value changed recently ({ev.source})")
            elif '"stable": false' in raw:
                ev.notes.append("editable source not stable across enough revisions")
        usable.append(ev)

    # ---- 2. establish official domains / orgs by independent source count
    support: dict[str, set[str]] = {}
    org_support: dict[str, set[str]] = {}
    for ev in usable:
        if not ev.supports:
            continue
        claim = _norm_ws(ev.claim + " " + ev.quote)
        src_e1 = etld1_of(host_of(ev.source))
        for d in sub.identity.official_domains:
            de1 = etld1_of(d)
            if de1 and (de1 in claim or d.lower() in claim):
                support.setdefault(de1, set()).add(src_e1)
        for platform, orgs in sub.identity.official_orgs.items():
            for org in orgs:
                key = f"{platform}:{org.lower()}"
                if org.lower() in claim:
                    org_support.setdefault(key, set()).add(src_e1)
    # a Wikipedia/Wikidata pair counts as one family
    def _distinct(srcs: set[str]) -> int:
        fam = set()
        for s in srcs:
            fam.add("wikimedia" if s in ("wikipedia.org", "wikidata.org") else s)
        return len(fam)

    established = [d for d, s in support.items() if _distinct(s) >= ic.min_sources]
    weak = [d for d, s in support.items() if 0 < _distinct(s) < ic.min_sources]
    est_orgs: dict[str, list[str]] = {}
    for key, s in org_support.items():
        platform, org = key.split(":", 1)
        # org may also inherit support if the org's page links to an established domain (bidirectional link)
        if _distinct(s) >= ic.min_sources or (_distinct(s) >= 1 and _bidirectional(org, platform, established, store)):
            est_orgs.setdefault(platform, []).append(org)
    # platform-verified link: org verified by the platform AND its website is an established domain
    for platform, orgs in sub.identity.official_orgs.items():
        for org in orgs:
            if org.lower() in [o.lower() for o in est_orgs.get(platform, [])]:
                continue
            if _platform_verified_link(org, platform, established, store):
                est_orgs.setdefault(platform, []).append(org.lower())
                notes.append(f"{platform} org '{org}' is platform-verified and links to an established official domain")
    # previously established identity (cache) is trusted until it expires
    if cached:
        for d in cached.get("official_domains", []):
            if d not in established:
                established.append(d)
                notes.append(f"official domain {d} taken from identity cache")
        for platform, orgs in (cached.get("official_orgs") or {}).items():
            for org in orgs:
                if org.lower() not in [o.lower() for o in est_orgs.get(platform, [])]:
                    est_orgs.setdefault(platform, []).append(org.lower())
                    notes.append(f"official {platform} org '{org}' taken from identity cache")
    for d in established:
        if d in support:
            notes.append(f"official domain {d} established by {_distinct(support[d])} independent source(s)")
    for d in weak:
        notes.append(f"official domain candidate {d} has only {_distinct(support[d])} independent source(s) (need {ic.min_sources})")
    for p, orgs in est_orgs.items():
        notes.append(f"official {p} org(s) established: {', '.join(orgs)}")

    # ---- 3. allowlist
    for entry in cfg.lists.allowlist:
        if entry.project in ("*", project) or entry.project.lower() == project.lower():
            if l0.host == entry.domain.lower() or l0.etld1 == etld1_of(entry.domain):
                notes.append(f"allowlist match: {entry.domain}")
                established.append(l0.etld1)

    if contradictions:
        notes.append("temporal contradiction: " + "; ".join(contradictions))
        l0.risk_signals.append("post_predates_domain")

    # ---- 4. fatal L0 failures
    fatal = l0.fatal_failures
    if fatal:
        msgs = "; ".join(f"{c.name}: {c.message}" for c in fatal)
        notes.append(f"fatal deterministic failure: {msgs}")
        return Decision(Verdict.FALSE, 0.9, notes, evidence, support, established, est_orgs)

    # ---- 5. TLS organization vs developer (only when both exist)
    dev = (sub.identity.developer or "").strip()
    if l0.tls_org and dev:
        if _org_matches(l0.tls_org, dev, sub.identity.aliases + [sub.identity.product]):
            notes.append(f"certificate Organization '{l0.tls_org}' matches developer '{dev}'")
            org_match = True
        else:
            notes.append(f"certificate Organization '{l0.tls_org}' does not match developer '{dev}'")
            org_match = False
    else:
        org_match = None

    # ---- 6. match target against established identity
    target_e1 = l0.etld1
    final_e1 = l0.final_etld1 or target_e1
    anchor = anchor_for(target_e1)
    in_official = target_e1 in established or (final_e1 in established and final_e1 == target_e1)
    if l0.final_etld1 and l0.final_etld1 != target_e1 and target_e1 in established and l0.final_etld1 not in established and not anchor_for(l0.final_etld1):
        notes.append(f"redirect leaves the official domain ({target_e1} -> {l0.final_etld1})")
        return Decision(Verdict.FALSE, 0.8, notes, evidence, support, established, est_orgs)

    risk_penalty = 0.05 * len(l0.risk_signals)
    if anchor and l0.platform_owner:
        owner = l0.platform_owner.lower()
        platform_orgs = [o.lower() for o in est_orgs.get(anchor.platform, [])]
        claimed_orgs = [o.lower() for o in sub.identity.official_orgs.get(anchor.platform, [])]
        if owner in platform_orgs:
            conf = min(0.95, 0.75 + 0.05 * len(usable)) - risk_penalty
            notes.append(f"path owner '{owner}' is the established official {anchor.platform} org")
            if l0.platform_repo:
                if _fork_of_other(l0, store):
                    notes.append("repository is a fork of another repository")
                    return Decision(Verdict.FALSE, 0.7, notes, evidence, support, established, est_orgs)
            return Decision(Verdict.TRUE, max(0.5, conf), notes, evidence, support, established, est_orgs)
        if platform_orgs and owner not in platform_orgs:
            notes.append(f"path owner '{owner}' differs from the established official {anchor.platform} org(s) {platform_orgs}")
            return Decision(Verdict.FALSE, 0.8, notes, evidence, support, established, est_orgs)
        if owner in claimed_orgs:
            notes.append(f"LLM claims '{owner}' is official on {anchor.platform} but independent support is insufficient")
        return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)

    if in_official:
        conf = min(0.95, 0.7 + 0.05 * len(usable)) - risk_penalty
        if org_match is True:
            conf = min(0.98, conf + 0.05)
        if org_match is False:
            notes.append("OV/EV certificate organisation mismatch overrides domain evidence")
            return Decision(Verdict.FALSE, 0.75, notes, evidence, support, established, est_orgs)
        notes.append(f"target domain {target_e1} is an established official domain")
        return Decision(Verdict.TRUE, max(0.5, conf), notes, evidence, support, established, est_orgs)

    if established:
        # we know the official domain(s), and the target is not one of them
        notes.append(f"target domain {target_e1} is not among the established official domain(s) {established}")
        conf = 0.7 + min(0.2, 0.05 * len(l0.risk_signals))
        if any(s.startswith(("typosquat", "brand_in_label", "new_domain")) for s in l0.risk_signals):
            conf = min(0.95, conf + 0.1)
        return Decision(Verdict.FALSE, conf, notes, evidence, support, established, est_orgs)

    if target_e1 in weak or target_e1 in [etld1_of(d) for d in sub.identity.official_domains]:
        notes.append("insufficient independent evidence to establish the official domain")
    else:
        notes.append("official identity could not be established from verifiable evidence")
    if sub.proposed_verdict == Verdict.FALSE.value:
        notes.append(f"investigator proposed FALSE: {sub.proposed_reason[:200]}")
    return Decision(Verdict.UNVERIFIABLE, 0.2, notes, evidence, support, established, est_orgs)


def _aged_enough(age: dict | None, ic, l0: L0Result, target_domain_age: dict | None, cfg: Config) -> tuple[bool, str]:
    """Deterministic promotion test for a tier-3 source."""
    if not age:
        return False, "no temporal provenance available"
    if not age.get("ok") or not age.get("created_ts"):
        return False, f"could not be dated ({age.get('error') or age.get('method')})"
    if age.get("strength") != "strong":
        return False, f"only a weak (self-reported) date via {age.get('method')} without Wayback corroboration"
    days = age.get("age_days") or 0
    if days < ic.tier3_min_age_days:
        return False, f"only {days} days old via {age['method']} (need {ic.tier3_min_age_days})"
    edited = age.get("edited_ts")
    if edited and (_age_days(edited) < ic.tier3_min_age_days):
        return False, f"created {days} days ago but edited {_age_days(edited)} days ago"
    # contradiction: the post predates the target domain's earliest evidence (only meaningful for young domains)
    if target_domain_age and target_domain_age.get("ok"):
        dom_days = target_domain_age.get("age_days") or 0
        if dom_days < cfg.identity.domain_age_contradiction_years * 365 and days > dom_days + 30:
            return False, f"predates the target domain ({days}d old post vs domain first seen {dom_days}d ago via {target_domain_age.get('source')})"
    return True, f"{days} days old via {age['method']}"


def _age_days(ts: float) -> int:
    import time
    return int((time.time() - ts) / 86400)


def _org_matches(cert_org: str, developer: str, aliases: list[str]) -> bool:
    def toks(s: str) -> set[str]:
        s = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|gmbh|ag|sa|srl|bv|oy|ab|plc|pty|the)\b\.?", " ", s.lower())
        return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) > 2}
    c = toks(cert_org)
    for cand in [developer] + aliases:
        t = toks(cand)
        if t and c and (t <= c or c <= t or len(t & c) >= max(1, min(len(t), len(c)) // 2 + (0 if len(c) == 1 else 1) - 1)):
            return True
    return False


def _bidirectional(org: str, platform: str, established: list[str], store: dict[str, str]) -> bool:
    """org page mentions an established official domain AND the official domain's content mentions the org."""
    org_l = org.lower()
    for d in established:
        org_text = " ".join(v.lower() for k, v in store.items() if org_l in k.lower() and platform in k.lower())
        site_text = " ".join(v.lower() for k, v in store.items() if etld1_of(host_of(k)) == d)
        if d in org_text and org_l in site_text:
            return True
    return False


def _platform_verified_link(org: str, platform: str, established: list[str], store: dict[str, str]) -> bool:
    """GitHub org with is_verified (DNS-verified domain) / HF verified org whose website is an established domain."""
    org_l = org.lower()
    for k, v in store.items():
        kl = k.lower()
        if platform not in kl or org_l not in kl:
            continue
        if '"is_verified": true' not in v.lower():
            continue
        m = re.search(r'"blog":\s*"([^"]+)"', v) or re.search(r'"website":\s*"([^"]+)"', v)
        if m and etld1_of(host_of(m.group(1))) in established:
            return True
    return False


def _fork_of_other(l0: L0Result, store: dict[str, str]) -> bool:
    key = f"https://github.com/{l0.platform_owner}/{l0.platform_repo}"
    for k, v in store.items():
        if k.lower().startswith(key.lower()) and '"fork": true' in v:
            return True
    return False
