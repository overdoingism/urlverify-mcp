"""Verdict rules engine. The LLM proposes; these rules verify and decide. The LLM cannot override them."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .cache.anchors import anchor_for, resolve as resolve_anchor
from .evidence import EvidenceStore, record_kind
from .checks.urltools import etld1_of, host_of
from .config import Config
from .cache.anchors import SEED
from .identity.sources import PLATFORM_FAMILIES, classify, family_of, source_key
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
    codes: list[str] = field(default_factory=list)            # fixed reason codes for a FALSE (what contradicts)
    established_edges: list[str] = field(default_factory=list)
    missing_edges: list[dict] = field(default_factory=list)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _squash(s: str) -> str:
    """Whitespace-free form for quote matching: JSON and HTML sources differ from the LLM's rendering only in spacing."""
    return re.sub(r"\s+", "", s or "").lower()


# Bumped whenever the rules change what they establish. Part of the identity-cache fingerprint, so identities
# established under older rules are re-verified instead of being trusted from the cache.
RULES_VERSION = "2026-09-24.3"
SELF_PUBLISHED_MAX_CONFIDENCE = 0.75   # TRUE for an owner established only by cross-platform consistency
STRUCTURED_KINDS = {"wikidata", "wikipedia", "github", "huggingface", "wayback", "package_registry", "distro", "flathub"}
_DOMAIN_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")


_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|…|\[\.\.\.\]|\[…\])\s*")


def _anchors(ev: Evidence) -> set[str]:
    """Fact anchors that must appear in the raw source output: domains, quoted strings ('x' or "x") and
    capitalised multi-word names from claim + quote. JSON keys are not anchors."""
    raw = ev.claim + " " + ev.quote
    text = _norm_ws(raw)
    out = set(_DOMAIN_RE.findall(text))
    for m in re.findall(r"'([^']{3,60})'", raw) + re.findall(r'"([^"]{3,60})"', raw):
        m = m.strip()
        if m and not re.fullmatch(r"[a-z0-9_]+", m):            # "developer_fields" is a key, "OpenJS Foundation" is a value
            out.add(m.lower())
    for m in re.findall(r"\b([A-Z][\w.-]+(?:\s+[A-Z][\w.-]+)+)\b", ev.claim):
        out.add(m.lower())
    return {a for a in out if len(a) >= 3}


def _fragments(q: str) -> list[str]:
    """A quote may elide with '...' — each remaining fragment must be found verbatim (whitespace ignored)."""
    return [f for f in (_squash(x) for x in _ELLIPSIS_RE.split(q)) if f]


def verify_quotes(evidence: list[Evidence], store: dict[str, str]) -> None:
    """Mark evidence.verified_quote and pin each evidence to what the tools actually fetched.

    - Fact citations (ev.facts = ["F12", ...]): the facts must exist; the evidence's source, kind and quote are
      rewritten from the structured record (the LLM's own source / quote text is not used).
    - Structured evidence without fact ids (legacy): fact-anchored against the cited record, found by any of its URLs.
    - Pages: the quote must appear verbatim (whitespace ignored) in the page fetched from that URL. A structured record
      for the same URL is a different thing and is never used to verify a page quote, and vice versa."""
    es = store if isinstance(store, EvidenceStore) else None
    for ev in evidence:
        if ev.facts and es is not None:
            known = [f for f in ev.facts if f in es.facts]
            if not known:
                ev.verified_quote = False
                ev.notes.append("cited fact ids do not exist; evidence discarded")
                continue
            src = es.facts[known[0]][0]
            same = [f for f in known if es.facts[f][0] == src]
            if len(same) < len(ev.facts):
                ev.notes.append(f"facts from other records or unknown ids ignored: {sorted(set(ev.facts) - set(same))}")
            ident = [f for f in es.identity_facts(src) if f not in same]
            ev.facts = same
            ev.source, ev.kind = src, es.kinds[src]
            ev.quote = es.fact_text(ident + same)
            ev.verified_quote = True
            ev.notes.append(f"structured facts {', '.join(same)} of record {es.record_ids.get(src)}")
            continue
        rec = es.find_record(ev.source) if es is not None else None
        page = es.page_text(ev.source) if es is not None else store.get(ev.source)
        use_record = rec is not None and (ev.kind in STRUCTURED_KINDS or page is None)
        if not use_record and page is None:
            ev.verified_quote = False
            ev.notes.append("cited URL was not fetched; evidence discarded")
            continue
        text = _squash(dict.get(store, rec) if use_record else page)
        frags = _fragments(ev.quote)
        quote_ok = sum(map(len, frags)) >= 8 and all(f in text for f in frags)
        if use_record:
            # Every anchor must match THIS record. A single real domain cannot legitimise
            # a fabricated domain/org elsewhere in the same proposed quote.
            anchors = {_squash(a) for a in _anchors(ev)}
            anchor_ok = bool(anchors) and all(a in text for a in anchors)
            ev.verified_quote = quote_ok or anchor_ok
            if ev.verified_quote and not quote_ok:
                # Voting uses only fetched facts, never unverified prose surrounding anchors.
                ev.quote = " ... ".join(sorted(a for a in _anchors(ev) if _squash(a) in text))
            ev.source, ev.kind = rec, es.kinds[rec]
        else:
            ev.verified_quote = quote_ok
            if ev.kind in STRUCTURED_KINDS and ev.kind != "distro":
                ev.kind = "page"
        if not ev.verified_quote:
            ev.notes.append("claimed quote/facts not found in the cited source; evidence discarded")


def decide(cfg: Config, l0: L0Result, sub: LLMSubmission, store: dict[str, str], project: str,
           cached: dict | None = None, ages: dict[str, dict] | None = None,
           target_domain_age: dict | None = None, provenance: dict | None = None,
           registry_state: dict | None = None) -> Decision:
    ctx: dict = {}
    d = _decide(cfg, l0, sub, store, project, cached, ages, target_domain_age, provenance, registry_state, ctx)
    d.established_edges, d.missing_edges = identity_edges(cfg, l0, d, ctx, provenance)
    return d


def target_kind(l0: L0Result) -> str:
    if l0.platform in ("pypi", "npm", "nuget") and l0.platform_owner:
        return "package"
    if l0.platform_scope == "user_content" and l0.platform_owner:
        return "platform"
    return "website"


def identity_edges(cfg: Config, l0: L0Result, d: "Decision", ctx: dict, provenance: dict | None) -> tuple[list[str], list[dict]]:
    """Which identity edges this decision established and which are missing (AGENTS.md §6.3). Pure bookkeeping over the
    decision's own state: it never changes the verdict."""
    need = cfg.identity.min_sources
    fam = lambda srcs: len({family_of(x) for x in srcs})   # noqa: E731
    est: list[str] = []
    miss: list[dict] = []

    def add(edge: str, ok: bool, have: int | None = None, why: str = "") -> None:
        if ok:
            est.append(edge)
        else:
            m = {"edge": edge}
            if have is not None:
                m.update(have=have, need=need)
            if why:
                m["why"] = why
            miss.append(m)

    add("TARGET_CHECKS", not l0.fatal_failures and not l0.incomplete_required_checks,
        why=", ".join(sorted({c.name for c in l0.fatal_failures} | set(l0.incomplete_required_checks))))
    kind = target_kind(l0)
    if kind == "website":
        e1 = l0.etld1
        add(f"PROJECT_TO_DOMAIN:{e1}", e1 in d.established_domains, fam(d.supporting_sources.get(e1, set())))
    else:
        owner = (l0.platform_owner or "").lower()
        org_support = ctx.get("org_support", {})
        add(f"PROJECT_TO_ORG:{l0.platform}:{owner}", owner in [o.lower() for o in d.established_orgs.get(l0.platform or "", [])],
            fam(org_support.get(f"{l0.platform}:{owner}", set())))
        if kind == "package" and l0.platform in ("pypi", "npm"):
            add("PACKAGE_TO_REPOSITORY", bool(provenance and provenance.get("found")),
                why="" if provenance and provenance.get("found") else "no signed build provenance")
    if ctx.get("project_ok") is not None:
        add("PROJECT_NAME_MATCH", bool(ctx["project_ok"]))
    return est, miss


def _decide(cfg: Config, l0: L0Result, sub: LLMSubmission, store: dict[str, str], project: str,
            cached: dict | None, ages: dict[str, dict] | None, target_domain_age: dict | None, provenance: dict | None,
            registry_state: dict | None, ctx: dict) -> Decision:
    notes: list[str] = []
    ic = cfg.identity
    ages = ages or {}
    promoted = 0
    contradictions: list[str] = []

    # ---- 1. evidence hygiene
    evidence = list(sub.evidence)
    verify_quotes(evidence, store)
    # hosting platforms are not identities: github.com in official_domains would make every GitHub page "self" and
    # would be counted as a domain to establish. Owners on platforms are handled through official_orgs.
    # ... unless the target IS the platform company's own site (desktop.docker.com, desktop.github.com: no path owner):
    # then that domain is the developer's identity and stays a candidate.
    platform_roots = {d for a in SEED for d in a.etld1s}
    if l0.platform_scope == "company_site":
        platform_roots.discard(l0.etld1)
    official_domains: list[str] = []
    for d in sub.identity.official_domains:
        if etld1_of(d) in platform_roots:
            if f"'{d}' is a hosting platform, not an identity; ignored as official domain (owners go in official_orgs)" not in notes:
                notes.append(f"'{d}' is a hosting platform, not an identity; ignored as official domain (owners go in official_orgs)")
        elif d not in official_domains:
            official_domains.append(d)
    self_domains = {etld1_of(d) for d in official_domains}
    usable: list[Evidence] = []
    for ev in evidence:
        # a structured tool record (JSON we fetched from an API) is platform data, not a page anyone could have written
        api_record = record_kind(store, ev.source) in STRUCTURED_KINDS
        tier, why = classify(ev.source, ev.tier, ic, api_record=api_record)
        ev.tier = tier
        ev.notes.append(why)
        src_e1 = etld1_of(host_of(ev.source))
        if not ev.verified_quote:
            continue
        if not api_record and _is_self(ev.source, l0, self_domains):
            ev.notes.append("self-attestation (the target's own pages / the candidate official domain); not counted")
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
        src_e1 = source_key(ev.source)
        if ev.kind == "wayback" or src_e1 == "archive.org":
            continue   # an archive snapshot proves age, not identity: no domain or org vote (temporal use is elsewhere)
        # votes come from the verified QUOTE only: the claim is the LLM's own text and may well say "not <x>"
        quote_text = _norm_ws(ev.quote)
        for d in official_domains:
            de1 = etld1_of(d)
            if de1 and (_domain_mentioned(quote_text, de1) or _domain_mentioned(quote_text, d.lower())):
                support.setdefault(de1, set()).add(src_e1)
        for platform, orgs in sub.identity.official_orgs.items():
            for org in orgs:
                key = f"{platform}:{org.lower()}"
                if _mentions(quote_text, org.lower()):
                    org_support.setdefault(key, set()).add(src_e1)
    # a stable Wikimedia "source code repository" record (Wikidata P1324 / infobox repo) names the official org on a
    # hosting platform; read from the raw tool output of evidence the LLM cited, independent of what it quoted
    for ev in usable:
        if not ev.supports or ev.kind not in ("wikidata", "wikipedia"):
            continue
        for platform, orgs in sub.identity.official_orgs.items():
            for org in orgs:
                if _wikimedia_repo_names_org(org, platform, ev.source, store):
                    org_support.setdefault(f"{platform}:{org.lower()}", set()).add(etld1_of(host_of(ev.source)))
                    notes.append(f"{ev.kind} records a stable official repository under {platform} org '{org}' ({ev.source})")
    # Flathub states that an unverified app is "not verified by, affiliated with, or supported by" the developer: for
    # such an app, nothing from the Flathub family (its records, pages, build manifests) counts as support
    for key in list(org_support):
        platform, org = key.split(":", 1)
        if platform == "flathub" and not _flathub_verified(org, store):
            kept = {x for x in org_support[key] if family_of(x) != "flathub"}
            if kept != org_support[key]:
                notes.append(f"Flathub does not verify '{org}' (community packaging): Flathub's own records and manifests are not counted for it")
            org_support[key] = kept
    # a Wikipedia/Wikidata pair counts as one family
    def _distinct(srcs: set[str]) -> int:
        return len({family_of(s) for s in srcs})

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
                org_support.setdefault(f"{platform}:{org.lower()}", set()).add("platform-verified-domain")   # basis beyond platform consistency
                notes.append(f"{platform} org '{org}' is platform-verified and links to an established official domain")
    # signed build provenance: the registry's own statement of which repository's CI published the package.
    # If that repository's owner is an established (or domain-verified, established-domain-linked) GitHub org, the
    # package owner on the registry is established too. Metadata links alone never do this.
    if provenance and provenance.get("found") and l0.platform in ("pypi", "npm") and l0.platform_owner:
        powner = provenance["repo"][0].lower()
        gh_est = [o.lower() for o in est_orgs.get("github", [])]
        blog = provenance.get("owner_blog") or ""
        blog_ok = provenance.get("owner_verified") and etld1_of(host_of(blog)) in established if blog else False
        scope = l0.platform_owner.split("/", 1)[0][1:] if l0.platform == "npm" and l0.platform_owner.startswith("@") else None
        scope_ok = scope is None or scope.lower() == powner
        if not scope_ok:
            notes.append("npm scope differs from provenance owner; package identity not inherited")
        if scope_ok and (powner in gh_est or blog_ok):
            est_orgs.setdefault(l0.platform, []).append(l0.platform_owner.lower())
            org_support.setdefault(f"{l0.platform}:{l0.platform_owner.lower()}", set()).add("signed-provenance")
            notes.append(f"{l0.platform} package '{l0.platform_owner}' established by signed build provenance from {provenance.get('repo_url')} "
                         f"(owner '{powner}' is an established GitHub org)" + ("; corroborated by deps.dev" if provenance.get("depsdev_verified") else ""))
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

    ctx["org_support"] = org_support
    # ---- 3. allowlist
    for entry in cfg.lists.allowlist:
        if entry.project in ("*", project) or entry.project.lower() == project.lower():
            if l0.host == entry.domain.lower() or l0.etld1 == etld1_of(entry.domain):
                notes.append(f"allowlist match: {entry.domain}")
                established.append(l0.etld1)

    if contradictions:
        notes.append("temporal contradiction: " + "; ".join(contradictions))
        l0.risk_signals.append("post_predates_domain")

    # ---- 3b. the registry's own statement about the target name overrides everything (any path, any mode)
    if registry_state and registry_state.get("state") in ("missing", "security_holding"):
        st = registry_state["state"]
        msg = (f"{l0.platform} package '{l0.platform_owner}' does not exist" if st == "missing" else
               f"{l0.platform} package '{l0.platform_owner}' is a security holding package ({registry_state.get('version')}): "
               "the name was taken over by the npm security team after a malicious package was removed")
        notes.append("registry state: " + msg)
        l0.risk_signals.append(f"registry_{st}")
        return Decision(Verdict.FALSE, 0.95 if st == "security_holding" else 0.9, notes, evidence, support, established, est_orgs, codes=["PACKAGE_SECURITY_HOLDING" if st == "security_holding" else "PACKAGE_NOT_FOUND"])
    if registry_state and registry_state.get("state") == "latest_yanked":
        notes.append(f"registry state: latest release {registry_state.get('version')} is fully yanked")
        l0.risk_signals.append("registry_latest_yanked")

    # ---- 4. fatal L0 failures
    fatal = l0.fatal_failures
    if fatal:
        msgs = "; ".join(f"{c.name}: {c.message}" for c in fatal)
        notes.append(f"fatal deterministic failure: {msgs}")
        return Decision(Verdict.FALSE, 0.9, notes, evidence, support, established, est_orgs, codes=["L0_FATAL"])

    if registry_state and registry_state.get("state") == "unknown":
        notes.append("target registry state unavailable; cannot establish the target release")
        return Decision(Verdict.UNVERIFIABLE, 0.2, notes, evidence, support, established, est_orgs)

    incomplete = l0.incomplete_required_checks
    if incomplete:
        notes.append("required deterministic checks incomplete: " + ", ".join(sorted(set(incomplete))))
        return Decision(Verdict.UNVERIFIABLE, 0.2, notes, evidence, support, established, est_orgs)

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

    # ---- 5b. the resolved identity must be about the requested project (asked for "requests", got pypdf's identity)
    project_ok, why_project = _project_matches(project, l0, usable)
    ctx["project_ok"] = project_ok
    if not project_ok:
        notes.append(f"{why_project}; VERIFIED_TRUE withheld")

    # ---- 5b. an asset / CDN host on a platform carries no owner in its URL: it cannot be verified on its own
    if l0.platform_scope == "user_content" and not l0.platform_owner:
        notes.append(f"{l0.host} is an asset / CDN host of {l0.platform} whose owner cannot be read from the URL; verify the "
                     "release or page that links to it instead")
        return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)

    # ---- 6. match target against established identity
    target_e1 = l0.etld1
    final_e1 = l0.final_etld1 or target_e1
    anchor = anchor_for(target_e1)
    in_official = target_e1 in established or (final_e1 in established and final_e1 == target_e1)
    if l0.final_etld1 and l0.final_etld1 != target_e1 and target_e1 in established and l0.final_etld1 not in established and not anchor_for(l0.final_etld1):
        # Mirror networks and download CDNs (SourceForge, get.videolan.org, Apache closer.lua, ftpmirror.gnu.org ...)
        # redirect away from the official domain by design: a host we cannot establish is "not confirmed", not a
        # counterfeit. (Was VERIFIED_FALSE before 2026-09-24.)
        notes.append(f"redirect leaves the official domain for a host that is not established ({target_e1} -> {l0.final_etld1}); "
                     "mirror networks do this by design, so the final host is unconfirmed rather than counterfeit")
        return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs, codes=["REDIRECT_TO_UNESTABLISHED_HOST"])

    # A known hosting platform does not authenticate the redirect's path owner.
    if l0.final_url and l0.final_url != l0.normalized_url:
        fh = host_of(l0.final_url)
        fa = anchor_for(etld1_of(fh))
        if fa:
            scope, owner, _ = resolve_anchor(fa, fh, urlsplit(l0.final_url).path)
            same_owner = (fa.platform == l0.platform and owner and l0.platform_owner
                          and owner.lower() == l0.platform_owner.lower())
            if scope == "user_content" and owner and not same_owner and owner.lower() not in [o.lower() for o in est_orgs.get(fa.platform, [])]:
                notes.append(f"redirect owner '{owner}' on {fa.platform} has not been established")
                return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)
    source_count = len({family_of(source_key(e.source)) for e in usable if e.supports})
    risk_penalty = 0.05 * len(l0.risk_signals)
    if anchor and l0.platform_owner:
        owner = l0.platform_owner.lower()
        platform_orgs = [o.lower() for o in est_orgs.get(anchor.platform, [])]
        claimed_orgs = [o.lower() for o in sub.identity.official_orgs.get(anchor.platform, [])]
        if owner in platform_orgs:
            conf = min(0.95, 0.75 + 0.05 * source_count) - risk_penalty
            conf = min(conf, (cached or {}).get("confidence_cap", 1.0))
            notes.append(f"path owner '{owner}' is the established official {anchor.platform} org")
            fams = {family_of(x) for x in org_support.get(f"{anchor.platform}:{owner}", set())}
            if fams and fams <= PLATFORM_FAMILIES:   # basis of THIS owner only; other entities' established domains are irrelevant
                conf = min(conf, SELF_PUBLISHED_MAX_CONFIDENCE)
                notes.append(f"self-published project: '{owner}' is established only by consistency across hosting platforms "
                             f"({', '.join(sorted(fams))}), with no Wikimedia, registry, media or own-domain evidence; "
                             f"confidence capped at {SELF_PUBLISHED_MAX_CONFIDENCE}")
            if l0.platform_repo:
                if _fork_of_other(l0, store):
                    notes.append("repository is a fork of another repository")
                    return Decision(Verdict.FALSE, 0.7, notes, evidence, support, established, est_orgs, codes=["REPOSITORY_IS_FORK"])
            if not project_ok:
                return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)
            return Decision(Verdict.TRUE, max(0.5, conf), notes, evidence, support, established, est_orgs)
        if platform_orgs and owner not in platform_orgs:
            notes.append(f"path owner '{owner}' differs from the established official {anchor.platform} org(s) {platform_orgs}")
            return Decision(Verdict.FALSE, 0.8, notes, evidence, support, established, est_orgs, codes=["OWNER_NOT_OFFICIAL"])
        if owner in claimed_orgs:
            notes.append(f"LLM claims '{owner}' is official on {anchor.platform} but independent support is insufficient")
        return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)

    if in_official:
        conf = min(0.95, 0.7 + 0.05 * source_count) - risk_penalty
        if org_match is True:
            conf = min(0.98, conf + 0.05)
        if org_match is False:
            notes.append("OV/EV certificate organisation mismatch overrides domain evidence")
            return Decision(Verdict.FALSE, 0.75, notes, evidence, support, established, est_orgs, codes=["CERT_ORG_MISMATCH"])
        conf = min(conf, (cached or {}).get("confidence_cap", 1.0))
        notes.append(f"target domain {target_e1} is an established official domain")
        if not project_ok:
            return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)
        return Decision(Verdict.TRUE, max(0.5, conf), notes, evidence, support, established, est_orgs)

    if established:
        # we know the official domain(s), and the target is not one of them
        notes.append(f"target domain {target_e1} is not among the established official domain(s) {established}")
        notes.append("the known identity list is not exhaustive; no explicit contradiction establishes a counterfeit")
        return Decision(Verdict.UNVERIFIABLE, 0.3, notes, evidence, support, established, est_orgs)

    if target_e1 in weak or target_e1 in [etld1_of(d) for d in official_domains]:
        notes.append("insufficient independent evidence to establish the official domain")
    else:
        notes.append("official identity could not be established from verifiable evidence")
    if sub.proposed_verdict == Verdict.FALSE.value:
        notes.append(f"investigator proposed FALSE: {sub.proposed_reason[:200]}")
    return Decision(Verdict.UNVERIFIABLE, 0.2, notes, evidence, support, established, est_orgs)


def _domain_mentioned(text: str, domain: str) -> bool:
    return bool(re.search(r"(?<![\w.-])(?:[a-z0-9-]+\.)*" + re.escape(domain) + r"(?![\w.-])", text, re.I))


def _mentions(text: str, value: str) -> bool:
    return bool(re.search(r"(?<![\w.-])" + re.escape(value) + r"(?![\w.-])", text, re.I))


def _owner_record(source: str, org: str, platform: str) -> bool:
    host = host_of(source)
    anchor = anchor_for(etld1_of(host))
    if not anchor or anchor.platform != platform:
        return False
    scope, owner, _ = resolve_anchor(anchor, host, urlsplit(source).path)
    return scope == "user_content" and bool(owner) and owner.lower() == org.lower()


def _is_self(source: str, l0: L0Result, self_domains: set[str]) -> bool:
    """Self-attestation: for a platform target, pages under the target owner's own paths on that platform
    (github.com/<owner>/..., raw.githubusercontent.com/<owner>/..., <owner>.github.io); other owners' pages on the same
    platform are not self (they are user content, tiered separately). For any target, pages on a candidate official
    domain are self. Platform roots themselves are never self."""
    host = host_of(source).lower()
    e1 = etld1_of(host)
    if l0.platform and l0.platform_owner and family_of(e1) == family_of(l0.etld1):
        owner = l0.platform_owner.lower()
        path = source.split("://", 1)[-1].split("/", 1)[1] if "/" in source.split("://", 1)[-1] else ""
        segs = [x for x in path.split("?", 1)[0].split("/") if x]
        return (bool(segs) and segs[0].lower() == owner) or host.startswith(owner + ".")
    if l0.platform and l0.platform_owner:
        return e1 in self_domains
    return e1 == l0.etld1 or e1 in self_domains


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


def _norm_name(x: str) -> str:
    return "".join(c for c in (x or "").casefold() if c.isalnum())


def _name_in(project_norm: str, text: str) -> bool:
    t = _norm_name(text)
    return bool(t) and len(project_norm) >= 3 and (project_norm in t or (len(t) >= 3 and t in project_norm))


def _project_matches(project: str, l0: L0Result, usable: list[Evidence]) -> tuple[bool, str]:
    """Deterministic check that the verified facts are about the project the caller asked for. The LLM's own
    `identity.product` text is deliberately NOT used: it is free text and drifts. Platform targets: the path owner or
    repository name must match. Website targets: at least one verified, supporting evidence quote must mention the name."""
    p = _norm_name(project)
    if not p:
        return True, "no project name given"
    if l0.platform and l0.platform_owner:
        for cand in (l0.platform_owner, l0.platform_repo or ""):
            if _name_in(p, cand):
                return True, f"project name matches the {l0.platform} path ({cand})"
        # the target's OWN platform record may carry its display name (Flathub: com.obsproject.Studio = "OBS Studio");
        # only that record counts, never another page or record that merely mentions the name
        for ev in usable:
            if ev.supports and ev.verified_quote and ev.kind == l0.platform and _owner_record(ev.source, l0.platform_owner, l0.platform):
                names = re.findall(r"\.(?:owner_info\.name|repo_info\.name|name) = ([^;]+)", ev.quote)
                if any(_norm_name(n) == p for n in names):
                    return True, f"project name is the display name in the target's own {l0.platform} record"
        return False, f"project '{project}' matches neither the {l0.platform} owner '{l0.platform_owner}' nor the repository '{l0.platform_repo}'"
    for ev in usable:
        if ev.supports and ev.verified_quote and _name_in(p, ev.quote):
            return True, f"project name appears in verified evidence from {host_of(ev.source)}"
    return False, f"no verified evidence mentions the project '{project}'"


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
        org_text = " ".join(v.lower() for k, v in store.items() if _owner_record(k, org, platform))
        site_text = " ".join(v.lower() for k, v in store.items() if etld1_of(host_of(k)) == d)
        if _mentions(org_text, d) and _mentions(site_text, f"{_PLATFORM_HOSTS.get(platform, platform)}/{org_l}"):
            return True
    return False


_PLATFORM_HOSTS = {"github": "github.com", "huggingface": "huggingface.co", "gitlab": "gitlab.com"}


def _wikimedia_repo_names_org(org: str, platform: str, source: str, store: dict[str, str]) -> bool:
    """True when the Wikidata entity / Wikipedia article at `source` lists an official repository under `org` on
    `platform` and that repository value has been stable across the history window."""
    host = _PLATFORM_HOSTS.get(platform)
    raw = store.get(source)
    if not host or not raw or record_kind(store, source) not in ("wikipedia", "wikidata"):
        return False
    try:
        data = json.loads(raw)
    except Exception:
        return False
    records = [e for e in data.get("entities", []) if e.get("source") == source] if isinstance(data.get("entities"), list) else [data]
    prefix = f"{host}/{org.lower()}"
    for rec in records:
        stab = rec.get("repo_stability") or {}
        if not stab.get("stable") or stab.get("recent_change"):
            continue
        for repo in rec.get("official_repos") or []:
            r = str(repo).lower()
            if r == prefix or r.startswith(prefix + "/"):
                return True
    return False


def _platform_verified_link(org: str, platform: str, established: list[str], store: dict[str, str]) -> bool:
    """GitHub org with is_verified (DNS-verified domain) / HF verified org whose website is an established domain."""
    for k, v in store.items():
        if record_kind(store, k) != platform or not _owner_record(k, org, platform):
            continue
        if '"is_verified": true' not in v.lower():
            continue
        m = re.search(r'"blog":\s*"([^"]+)"', v) or re.search(r'"website":\s*"([^"]+)"', v)
        if m and etld1_of(host_of(m.group(1))) in established:
            return True
    return False


def _flathub_verified(app_id: str, store: dict[str, str]) -> bool:
    """True only when a Flathub record for the app says it is developer-verified."""
    for k, v in store.items():
        if record_kind(store, k) != "flathub" or not _owner_record(k, app_id, "flathub"):
            continue
        try:
            return bool((json.loads(v).get("owner_info") or {}).get("is_verified"))
        except (ValueError, AttributeError):
            return False
    return False


def _fork_of_other(l0: L0Result, store: dict[str, str]) -> bool:
    key = f"https://github.com/{l0.platform_owner}/{l0.platform_repo}"
    for k, v in store.items():
        if record_kind(store, k) == "github" and k.lower().rstrip("/") == key.lower() and '"fork": true' in v:
            return True
    return False
