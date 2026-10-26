"""End-to-end verification: L0 -> (identity cache) -> L1 investigation -> rules -> reason in caller's language."""
from __future__ import annotations

import asyncio
import json
import re
import hashlib
import time
import uuid

from .agent.loop import Investigator
from .agent.prompts import reason_prompt
from . import health, progress
from .checks import apply_injection_check, run_l0
from .config import Config
from .identity.aging import Aging, age_many, domain_first_seen
from .identity.registry import RegistryFastPath
from .identity.releases import annotate_result, check_release
from .identity.sources import classify, tier1_path_prefixes
from .identity.structured import Structured
from .models import IdentityGraph, Verdict, VerifyRequest, VerifyResult
from .providers.llm import LLM
from .providers.fetch import make_fetcher
from .providers.search import make_search_provider
from .rules import decide
from .storage import Storage


async def verify(req: VerifyRequest, base_cfg: Config, store: Storage, record: bool = True) -> VerifyResult:
    t0 = time.time()
    cfg = base_cfg.with_overrides(req.options)
    trace_id = uuid.uuid4().hex[:12]
    from .promptstore import get_store
    get_store(base_cfg.prompts.dir)
    if health.HEALTH._store is not store:
        health.HEALTH.attach(store)
    dtoken = health.begin_collect()
    # Stage tracking works even without an MCP client (CLI / admin): bind a silent Progress if none is bound.
    ptoken = progress.bind(progress.Progress(None, events=False, heartbeat_s=0)) if progress.current() is None else None
    try:
        try:
            res = await asyncio.wait_for(_verify(req, cfg, store, trace_id, t0), timeout=cfg.budget.max_total_s)
            annotate_result(res, req)
            res.degraded = health.end_collect(dtoken)
            dtoken = None
            if res.degraded:
                res.engine_notes.append("degraded dependencies during this run: " + ", ".join(res.degraded))
            if record:
                store.add_history(trace_id, req.project, req.url, req.description, res.verdict.value, res.confidence, res.model_dump(mode="json"))
            return res
        except asyncio.TimeoutError:
            p = progress.current()
            stage = p.stage() if p else "unknown stage"
            reason = (f"UNVERIFIABLE: the verification exceeded the total time budget of {cfg.budget.max_total_s}s "
                      f"(last stage: {stage}). Increase budget.max_total_s or check the LLM / search endpoints.")
            result = VerifyResult(verdict=Verdict.UNVERIFIABLE, confidence=0.0, reason=reason,
                                  engine_notes=[f"total deadline {cfg.budget.max_total_s}s exceeded at: {stage}"],
                                  trace_id=trace_id, duration_s=round(time.time() - t0, 1), path="timeout")
            if record:
                store.add_history(trace_id, req.project, req.url, req.description, result.verdict.value, 0.0, result.model_dump(mode="json"))
            return result
    finally:
        if dtoken is not None:
            health.end_collect(dtoken)
        if ptoken is not None:
            progress.unbind(ptoken)


MAX_ENGINE_FETCHES = 3


async def fetch_cited(sub, store, fetcher, l0, cfg: Config, notes: list[str], limit: int = MAX_ENGINE_FETCHES) -> list[str]:
    """Third-party pages the investigator quoted without fetching would be discarded as unverifiable. The engine fetches
    a few of them itself (tier 1 / 2 only; never the target's own site or a candidate official domain, which do not
    count anyway) so the quote can be checked verbatim against what is really there. Nothing is relaxed: the quote must
    still match, and the fetched text is never shown to the LLM. (AGENTS §6.3; the Vulkan SDK / Chocolatey case.)"""
    from .checks.urltools import etld1_of, host_of
    own = {etld1_of(d) for d in sub.identity.official_domains} | {l0.etld1}
    todo: list[str] = []
    for ev in sub.evidence:
        url = ev.source or ""
        if ev.facts or not ev.quote or not url.startswith(("http://", "https://")) or url in todo:
            continue
        if store.page_text(url) is not None or store.find_record(url) is not None:
            continue
        if etld1_of(host_of(url)) in own or classify(url, ev.tier, cfg.identity)[0] > 2:
            continue
        todo.append(url)
    fetched = []
    for url in todo[:limit]:
        try:
            store[url] = await fetcher.fetch(url)
            fetched.append(url)
        except Exception as e:  # noqa: BLE001
            notes.append(f"engine fetch of cited page {url} failed: {type(e).__name__}")
    if fetched:
        notes.append("engine fetched pages the investigator cited without fetching (quotes are checked verbatim): " + ", ".join(fetched))
    return fetched


def identity_basis(dec) -> dict:
    """The source families each established domain / org rests on, stored with the cached identity so a later run
    that reads it back judges the same basis (e.g. not "self-published" when media evidence established it)."""
    from .identity.sources import family_of
    fam = lambda srcs: sorted({family_of(x) for x in srcs})   # noqa: E731
    orgs = {f"{p}:{o.lower()}" for p, os_ in dec.established_orgs.items() for o in os_}
    return {"domains": {d: fam(dec.supporting_sources.get(d, set())) for d in dec.established_domains if dec.supporting_sources.get(d)},
            "orgs": {k: fam(v) for k, v in dec.org_supporting_sources.items() if k in orgs and v}}


def _package_from_l0(l0) -> dict | None:
    """A registry URL target (pypi.org/project/x, npmjs.com/package/x, nuget.org/packages/x, files.pythonhosted.org ...):
    the release-cooldown lookup already resolved registry, package and version; else the anchor owner, version unknown."""
    rc = next((c for c in l0.checks if c.name == "release_cooldown"), None)
    d = rc.detail if rc is not None and isinstance(rc.detail, dict) else {}
    if d.get("registry") in ("pypi", "npm", "nuget") and d.get("package"):
        return {"ecosystem": d["registry"], "name": d["package"], "version": d.get("version")}
    if l0.platform in ("pypi", "npm", "nuget") and l0.platform_owner and l0.platform_scope == "user_content":
        return {"ecosystem": l0.platform, "name": l0.platform_owner, "version": None}
    return None


def identity_policy(cfg: Config) -> str:
    """Changing the evidence policy invalidates identities established under a different policy."""
    from .rules import RULES_VERSION
    policy = {"rules_version": RULES_VERSION,
              "identity": cfg.identity.model_dump(exclude={"github_token"}), "lists": cfg.lists.model_dump(),
              "tier1_paths": sorted(tier1_path_prefixes())}
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


async def _verify(req: VerifyRequest, cfg: Config, store: Storage, trace_id: str, t0: float) -> VerifyResult:
    cache_hits: list[str] = []
    policy = identity_policy(cfg)
    cached_identity = store.get_identity(req.project)
    if cached_identity and cached_identity.get("policy_fingerprint") != policy:
        cached_identity = None
    if cached_identity:
        cache_hits.append("identity")
    known_official = (cached_identity or {}).get("official_domains", [])

    await progress.report("L0: deterministic checks (TLS, DNS, redirects, host structure)", 0.02)
    l0 = await run_l0(req.url, cfg, store, known_official=known_official, cache_hits=cache_hits)
    await progress.report("L0 done" + (": fatal failure" if l0.fatal_failures else ""), 0.10)

    llm = LLM(cfg)
    search = make_search_provider(cfg)
    fetcher = make_fetcher(cfg, search)
    structured = Structured(max(cfg.net.timeout_s, 30), cfg.net.user_agent, cfg.identity.github_token,   # archive.org / wikimedia can be slow
                            cfg.net.retries, cfg.net.retry_backoff_s)
    inv = Investigator(cfg, llm, search, structured, fetcher)
    registry = RegistryFastPath(cfg, structured)
    engine_notes: list[str] = []
    try:
        if not l0.fatal_failures:
            await progress.report("checking registry release cooldown", 0.11)
            cooldown = await check_release(l0.normalized_url, cfg, structured.client)
            if cooldown is None and l0.final_url:
                cooldown = await check_release(l0.final_url, cfg, structured.client)
            if cooldown is not None:
                l0.checks.append(cooldown)
        # known malware / registry holds (AGENTS §6.5): before any path that could say TRUE
        pkg_sec = None
        pkg = req.package or _package_from_l0(l0)
        ps_cfg = cfg.package_security
        if pkg and not l0.fatal_failures and (ps_cfg.osv or ps_cfg.pypi_status):
            await progress.report("checking known-malware lists and registry status", 0.115)
            try:
                from .identity import package_security
                pkg_sec = await package_security.check(structured.client, pkg, ps_cfg.osv, ps_cfg.pypi_status)
                engine_notes.extend(pkg_sec["notes"])
            except Exception as e:  # noqa: BLE001
                engine_notes.append(f"package security lookup failed: {type(e).__name__}: {e}")
        pkg_block = (pkg_sec or {}).get("block")
        pkg_notices = list((pkg_sec or {}).get("notices") or [])
        # Always fetch the target page ourselves for injection screening (does not consume the LLM's budget).
        page = None
        page_source = l0.final_url or l0.normalized_url
        await progress.report("fetching target page for injection screening", 0.12)
        try:
            if l0.fatal_failures or l0.target_missing:
                raise ValueError("target fetch skipped: " + ("fatal L0 failure" if l0.fatal_failures else "the host does not exist"))
            page = await fetcher.fetch(page_source)
            page_source = getattr(fetcher, "sources", {}).get(page_source, page_source)
        except Exception as e:  # noqa: BLE001
            engine_notes.append(f"target page fetch ({cfg.fetch.provider}) failed: {type(e).__name__}: {e}")
            if cfg.fetch.provider != "builtin" and not l0.fatal_failures and not l0.target_missing:
                from .providers.fetch import BuiltinFetcher
                bf = BuiltinFetcher(cfg)
                try:
                    page = await bf.fetch(page_source)
                    page_source = bf.sources.get(page_source, page_source)
                    engine_notes.append("target page fetched with the built-in fetcher instead")
                except Exception as e2:  # noqa: BLE001
                    engine_notes.append(f"built-in target page fetch failed: {type(e2).__name__}: {e2}")
                finally:
                    await bf.close()
        if page is not None:
            inv.target_page_text = page[: cfg.budget.fetch_max_chars]
            inv.evidence_store[page_source] = inv.target_page_text
        for seed in req.seeds:                      # e.g. the winget manifest that names this installer URL
            inv.evidence_store[seed["source"]] = seed["text"]
        apply_injection_check(l0, inv.target_page_text, cfg.injection_patterns)

        fp = cfg.package_registry_fast_path
        if not l0.fatal_failures and not pkg_block and fp.enabled and fp.mode != "full" and l0.platform in ("pypi", "npm"):
            await progress.report(f"registry fast path ({l0.platform})", 0.12)
            try:
                fast = await registry.run(l0, t0, trace_id, req.project)
            except Exception as e:  # noqa: BLE001
                fast = None
                engine_notes.append(f"registry fast path error: {type(e).__name__}: {e}")
            if fast is not None:
                fast.rule_notices = list(dict.fromkeys(fast.rule_notices + pkg_notices))
                fast.cache_hits = sorted(set(cache_hits))
                fast.engine_notes = engine_notes + fast.engine_notes
                await progress.report("done (registry fast path)", 1.0)
                return fast
            engine_notes.append("registry fast path inconclusive; running the full investigation")
            if fp.mode == "quick":
                res = VerifyResult(verdict=Verdict.UNVERIFIABLE, confidence=0.2, path="registry_fast_path",
                                   reason="UNVERIFIABLE: registry fast path was inconclusive and options.mode='quick' forbids the full investigation. "
                                          + "; ".join(engine_notes[-3:]),
                                   checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
                                   risk_signals=l0.risk_signals, cache_hits=sorted(set(cache_hits)), engine_notes=engine_notes,
                                   trace_id=trace_id, duration_s=round(time.time() - t0, 1), rule_notices=pkg_notices)
                return res

        # provenance / registry state do not depend on the LLM: computed first so the fixed lookups can use them
        prov = None
        registry_state = None
        if pkg_block:
            registry_state = {"state": pkg_block, "codes": pkg_sec["codes"], "notes": pkg_sec["notes"], "ids": pkg_sec["ids"]}
        elif not l0.fatal_failures and l0.platform in ("pypi", "npm") and l0.platform_owner:
            try:
                registry_state = await registry.registry_state(l0)
                sig = (registry_state or {}).get("signals")
                if sig and sig.get("exists"):
                    prov = await registry.provenance_signal(sig)
                    if prov.get("found"):
                        gh = await structured.github(prov["repo"][0])
                        oi = (gh.get("owner_info") or {}) if gh.get("ok") else {}
                        prov["owner_verified"] = bool(oi.get("is_verified"))
                        prov["owner_blog"] = oi.get("blog")
                        inv.evidence_store[prov.get("source") or "provenance"] = json.dumps(prov, default=str)
            except Exception as e:  # noqa: BLE001
                if registry_state is None:
                    registry_state = {"state": "unknown", "error": str(e)}
                engine_notes.append(f"provenance lookup failed: {type(e).__name__}: {e}")

        from .models import LLMSubmission
        fixed_codes: list[str] = []
        if l0.fatal_failures or l0.target_missing or pkg_block:
            # No need to spend LLM budget: deterministic failure is final / there is nothing at the target to verify.
            sub = LLMSubmission(identity=IdentityGraph(product=req.project))
            engine_notes.append("L1 skipped: " + ("fatal L0 failure" if l0.fatal_failures else
                                                  "the target host does not exist" if l0.target_missing else
                                                  "the package is known malware / held by its registry"))
        else:
            # ---- fixed, gap-driven lookups (AGENTS.md §6.3): no LLM involved
            from .identity.prefetch import Prefetch, brief, merge
            await progress.report("L1: fixed lookups (Wikimedia, platform records)", 0.14)
            pre = Prefetch(cfg, structured, inv.evidence_store)
            try:
                det_sub = await pre.run(l0, req.project)
            except Exception as e:  # noqa: BLE001
                det_sub = LLMSubmission(identity=IdentityGraph(product=req.project))
                engine_notes.append(f"fixed lookups failed: {type(e).__name__}: {e}")
            fixed_codes = list(pre.codes)
            det_dec = decide(cfg, l0, det_sub.model_copy(deep=True), inv.evidence_store, req.project, cached_identity,
                             {}, None, prov, registry_state)
            # a candidate official domain whose only family is Wikimedia: collect the single-source samples (AGENTS §4.1)
            solo_ok: list[str] = []
            if det_dec.verdict != Verdict.TRUE and cfg.identity.wikimedia_solo.enabled:
                from .identity.sources import family_of as _fam
                from .rules import _wikimedia_solo
                lone = [d for d, srcs in det_dec.supporting_sources.items()
                        if d not in det_dec.established_domains and {_fam(x) for x in srcs} == {"wikimedia"}]
                if lone:
                    try:
                        collected = await pre.wikimedia_solo(lone)
                    except Exception as e:  # noqa: BLE001
                        collected = []
                        engine_notes.append(f"single-source Wikimedia lookup failed: {type(e).__name__}: {e}")
                    solo_ok = [d for d in collected if _wikimedia_solo(d, inv.evidence_store, cfg.identity.wikimedia_solo)[0]]
                    if collected:
                        det_dec = decide(cfg, l0, det_sub.model_copy(deep=True), inv.evidence_store, req.project, cached_identity,
                                         {}, None, prov, registry_state)
            official = det_dec.established_domains + [d for d in solo_ok if d not in det_dec.established_domains]
            # website on a host that is not established while an official domain is (mirror / CDN), or a SourceForge
            # project: look at the official home page and its download page for a link to this exact file (or to the
            # SourceForge project) (AGENTS §6.4), then decide again
            if det_dec.verdict != Verdict.TRUE and official and (
                    (l0.platform_scope != "user_content" and l0.etld1 not in official)
                    or (l0.platform == "sourceforge" and l0.platform_owner)):
                try:
                    if await pre.official_pages(fetcher, official, l0.etld1):
                        det_dec = decide(cfg, l0, det_sub.model_copy(deep=True), inv.evidence_store, req.project, cached_identity,
                                         {}, None, prov, registry_state)
                except Exception as e:  # noqa: BLE001
                    engine_notes.append(f"official page lookup failed: {type(e).__name__}: {e}")
            engine_notes.extend(pre.notes)
            decisive = det_dec.verdict in (Verdict.TRUE, Verdict.FALSE) and fp.mode != "full"
            if decisive and "WIKIMEDIA_ONLY" in det_dec.notices and cfg.llm.enabled:
                decisive = False       # lowest-confidence path: let the investigator look for a second family first
            if decisive:
                sub = det_sub
                engine_notes.append(f"decided from fixed lookups without the LLM ({det_dec.verdict.value})")
            elif not cfg.llm.enabled:
                sub = det_sub
                engine_notes.append("no-LLM mode: stopped after the fixed lookups; the missing edges were not investigated")
            else:
                await progress.report("L1: identity investigation (LLM + tools) on the missing edges", 0.15)
                try:
                    llm_sub = await inv.investigate(req.project, req.url, req.description, l0, cached_identity,
                                                    gap_brief=brief(det_dec, inv.evidence_store, pre.ambiguous))
                    sub = merge(det_sub, llm_sub)
                    # third fixed candidate: the developer the LLM resolved, if Wikimedia has not been asked about it yet
                    dev = (llm_sub.identity.developer or "").strip()
                    if dev and await pre.wikimedia(dev, l0):
                        sub = merge(pre.submission(req.project), llm_sub)
                        engine_notes.append(f"fixed lookup after the LLM: Wikimedia for developer '{dev}'")
                    await fetch_cited(sub, inv.evidence_store, fetcher, l0, cfg, engine_notes)
                except Exception as e:  # noqa: BLE001
                    sub = det_sub
                    engine_notes.append(f"investigation failed: {type(e).__name__}: {e}")
                    # say so in the result: an UNVERIFIABLE because the LLM was down is not a judgement about the source
                    pkg_notices.append("LLM_UNAVAILABLE" if type(e).__module__.startswith("openai") else "INVESTIGATION_FAILED")
        if not l0.fatal_failures:
            from .models import Evidence
            for seed in req.seeds:
                if not any(e.source == seed["source"] for e in sub.evidence):
                    sub.evidence.append(Evidence(kind=seed["kind"], source=seed["source"], tier=1, claim=seed["claim"],
                                                 quote=seed["quote"], supports=True))
        # temporal provenance for tier-3 sources the LLM cited (deterministic; no LLM involved)
        ages: dict = {}
        target_age: dict | None = None
        from .rules import STRUCTURED_KINDS
        tier3_urls = [e.source for e in sub.evidence
                      if classify(e.source, e.tier, cfg.identity)[0] == 3 and e.kind not in STRUCTURED_KINDS]   # API/registry results are not pages to date
        if tier3_urls and not cfg.identity.allow_tier3:
            await progress.report(f"dating {len(tier3_urls)} tier-3 source(s)", 0.82)
            aging = Aging(max(cfg.net.timeout_s, 30), cfg.net.user_agent, structured, cfg.identity.aging_sources)
            try:
                ct_detail = next((c.detail for c in l0.checks if c.name == "ct_first_seen"), None)
                ages, target_age = await asyncio.gather(
                    age_many(aging, list(dict.fromkeys(tier3_urls))),
                    domain_first_seen(l0.etld1, ct_detail, structured) if not l0.platform else asyncio.sleep(0, result=None))
            except Exception as e:  # noqa: BLE001
                engine_notes.append(f"aging lookup failed: {type(e).__name__}: {e}")
            finally:
                await aging.close()
            for u, a in ages.items():
                engine_notes.append(f"aging {u[:80]}: " + (f"{a.get('age_days')}d via {a.get('method')} ({a.get('strength')})" if a.get("created_ts") else f"undated ({a.get('error')})"))
        await progress.report("rules engine: verifying evidence and deciding", 0.88)
        dec = decide(cfg, l0, sub, inv.evidence_store, req.project, cached_identity, ages, target_age, prov, registry_state)
        from .devtools.capture import capture
        capture(trace_id, req.project, req.url, cfg, l0, sub, inv.evidence_store, cached_identity, ages, target_age, prov, registry_state, dec)
        engine_notes.extend(dec.notes)
        if dec.verdict == Verdict.TRUE and any(c.name == "injection" and c.status == "skip" for c in l0.checks):
            dec.confidence = max(0.5, dec.confidence - 0.1)
            engine_notes.append("injection screening could not run (target page not fetched); confidence reduced by 0.1")

        # identity cache: only persist when independently established
        if not cached_identity and dec.verdict == Verdict.TRUE and (dec.established_domains or dec.established_orgs):
            store.put_identity(req.project, {
                "developer": sub.identity.developer, "aliases": sub.identity.aliases,
                "official_domains": sorted(set(dec.established_domains)), "official_orgs": dec.established_orgs,
                "evidence": [e.model_dump() for e in dec.evidence if e.verified_quote],
                "established_at": time.time(), "policy_fingerprint": policy, "confidence_cap": dec.confidence,
                "basis_notices": [n for n in dec.notices if n == "WIKIMEDIA_ONLY"],
                "basis": identity_basis(dec),
            }, cfg.cache.identity_ttl_hours * 3600)

        await progress.report(f"decision {dec.verdict.value}; writing reason", 0.92)
        if cfg.llm.enabled and not (pkg_block or l0.target_missing):     # those are fully stated by their codes
            reason = await _write_reason(llm, req, dec.verdict, dec.confidence, engine_notes, l0, sub, dec)
        elif (pkg_block or l0.target_missing) and dec.notes:
            reason = f"{dec.verdict.value}: {dec.notes[-1]}"          # the deciding note is the last one written
        else:
            reason = f"{dec.verdict.value}: " + "; ".join(n for n in dec.notes[:6])
        await progress.report("done", 1.0)
        result = VerifyResult(
            verdict=dec.verdict, confidence=round(dec.confidence, 2), reason=reason,
            evidence=dec.evidence, checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
            identity=sub.identity, risk_signals=l0.risk_signals + sub.risk_notes, cache_hits=sorted(set(cache_hits)),
            engine_notes=engine_notes, trace_id=trace_id, duration_s=round(time.time() - t0, 1),
            path="l0_fatal" if l0.fatal_failures else "package_security" if pkg_block else "full",
            established_edges=dec.established_edges, missing_edges=dec.missing_edges,
            rule_notices=list(dict.fromkeys(dec.notices + pkg_notices)),
            rule_codes=dec.codes + ([c for c in fixed_codes if c not in dec.codes] if dec.verdict != Verdict.TRUE else []),
        )
    finally:
        await fetcher.close()
        await search.close()
        await structured.close()
    return result


def counted_evidence(dec) -> tuple[list[str], int]:
    """The evidence the rules actually counted for the established identity, one line per source family, and the
    number of families. Discarded, excluded and self-attesting evidence is not listed (AGENTS §6.1)."""
    from .identity.sources import family_of, source_key
    fams: set[str] = set()
    for d in dec.established_domains:
        fams |= {family_of(x) for x in dec.supporting_sources.get(d, set())}
    for p, orgs in dec.established_orgs.items():
        for o in orgs:
            fams |= {family_of(x) for x in dec.org_supporting_sources.get(f"{p}:{o.lower()}", set())}
    lines, seen = [], set()
    for ev in dec.evidence:
        if not (ev.verified_quote and ev.supports) or ev.source in seen:
            continue
        f = family_of(source_key(ev.source))
        if f in fams and not any("not counted" in n or "excluded" in n for n in ev.notes):
            seen.add(ev.source)
            lines.append(f"- [{f}] {ev.source}: {(ev.quote or ev.claim)[:240]}")
    named = {ln.split("]")[0][3:] for ln in lines}
    lines += [f"- [{f}] (basis recorded with the cached identity)" for f in sorted(fams - named)]
    return lines, len(fams)


_FILE_EXT = {"md", "py", "exe", "zip", "json", "yaml", "yml", "txt", "sh", "ps1", "cpp", "h", "gz", "tar", "msi", "dmg",
             "deb", "rpm", "whl", "js", "ts", "toml", "cfg", "ini", "rs", "so", "dll", "bin", "gguf", "safetensors"}
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
              "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _hosts_in(text: str) -> set[str]:
    from .checks.urltools import etld1_of, _extract
    out = set()
    for tok in re.findall(r"(?i)\b((?:[a-z0-9-]+\.)+[a-z]{2,})\b", text or ""):
        t = tok.lower()
        if t.rsplit(".", 1)[-1] in _FILE_EXT or not _extract(t).suffix:
            continue
        out.add(etld1_of(t))
    return out


def reason_problems(text: str, prompt: str, n_counted: int) -> list[str]:
    """Deterministic check of the LLM-written reason: it may not name a website its input did not contain, and any
    number of sources it states must be the number the rules counted."""
    bad = sorted(_hosts_in(text) - _hosts_in(prompt))
    probs = [f"names {', '.join(bad)}, which its input did not contain"] if bad else []
    for m in re.finditer(r"(?i)\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:independent\s+|separate\s+|distinct\s+)?(?:source|sources|source families)\b"
                         r"|(\d+|[一二兩三四五六七八九十])\s*(?:個|家)\s*(?:獨立|不同)?\s*(?:來源|來源家族)", text or ""):
        word = (m.group(1) or m.group(2) or "").lower()
        n = int(word) if word.isdigit() else _NUM_WORDS.get(word)
        if n is not None and n != n_counted:
            probs.append(f"states {n} sources, the rules counted {n_counted}")
    return probs


async def _write_reason(llm: LLM, req: VerifyRequest, verdict: Verdict, confidence: float, notes: list[str], l0, sub, dec=None) -> str:
    """The LLM writes the explanation from what the rules decided: their notes, the checks and the counted evidence only.
    The investigator's narrative is NOT given (it holds the LLM's own unverified claims); the text is then checked."""
    findings = "\n".join(f"- {n}" for n in notes)
    findings += "\n" + "\n".join(f"- check {c.name}: {c.status}{' (' + c.message + ')' if c.message else ''}" for c in l0.checks)
    lines, n = counted_evidence(dec) if dec is not None else ([], 0)
    evidence = f"{n} independent source famil{'y' if n == 1 else 'ies'} counted.\n" + ("\n".join(lines) if lines else "- (none)")
    prompt = reason_prompt().format(project=req.project, url=req.url, description=req.description, verdict=verdict.value,
                                  confidence=confidence, findings=findings, evidence=evidence,
                                  narrative="(not provided: use the counted evidence)")   # older prompt overrides
    try:
        msg = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        text = re.sub(r"<think>.*?</think>", "", (msg.content or "").strip(), flags=re.S).strip()
        probs = reason_problems(text, prompt, n) if text else ["empty"]
        if not probs:
            return text
        notes.append("LLM-written explanation rejected (" + "; ".join(probs) + "); template text used")
    except Exception:  # noqa: BLE001
        pass
    return f"{verdict.value}: " + "; ".join(notes[:6])
