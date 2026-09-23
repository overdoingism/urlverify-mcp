"""End-to-end verification: L0 -> (identity cache) -> L1 investigation -> rules -> reason in caller's language."""
from __future__ import annotations

import asyncio
import json
import hashlib
import time
import uuid

from .agent.loop import Investigator
from .agent.prompts import reason_prompt
from . import health, progress
from .tracelog import TRACE, configure_from, reset_trace_id, set_trace_id
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
    configure_from(base_cfg)
    from .promptstore import get_store
    get_store(base_cfg.prompts.dir)
    if health.HEALTH._store is not store:
        health.HEALTH.attach(store)
    token = set_trace_id(trace_id)
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
            TRACE.log("verify_end", result=res.model_dump(mode="json"))
            return res
        except asyncio.TimeoutError:
            p = progress.current()
            stage = p.stage() if p else "unknown stage"
            reason = (f"UNVERIFIABLE: the verification exceeded the total time budget of {cfg.budget.max_total_s}s "
                      f"(last stage: {stage}). Increase budget.max_total_s or check the LLM / search endpoints.")
            TRACE.log("verify_timeout", max_total_s=cfg.budget.max_total_s, stage=stage)
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
        reset_trace_id(token)


def identity_policy(cfg: Config) -> str:
    """Changing the evidence policy invalidates identities established under a different policy."""
    policy = {"identity": cfg.identity.model_dump(exclude={"github_token"}), "lists": cfg.lists.model_dump(),
              "tier1_paths": sorted(tier1_path_prefixes())}
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


async def _verify(req: VerifyRequest, cfg: Config, store: Storage, trace_id: str, t0: float) -> VerifyResult:
    TRACE.log("verify_start", request=req.model_dump(), effective_config={"identity": cfg.identity.model_dump(), "budget": cfg.budget.model_dump(),
                                                                          "search_provider": cfg.search.provider, "llm_model": cfg.llm.model})
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
    TRACE.log("l0_result", result=l0.model_dump(exclude={"fetched_target_text"}), cached_identity=cached_identity)

    llm = LLM(cfg)
    search = make_search_provider(cfg)
    fetcher = make_fetcher(cfg, search)
    structured = Structured(max(cfg.net.timeout_s, 30), cfg.net.user_agent, cfg.identity.github_token)  # archive.org / wikimedia can be slow
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
                TRACE.log("release_cooldown", check=cooldown.model_dump())
        # Always fetch the target page ourselves for injection screening (does not consume the LLM's budget).
        page = None
        page_source = l0.final_url or l0.normalized_url
        await progress.report("fetching target page for injection screening", 0.12)
        try:
            if l0.fatal_failures:
                raise ValueError("target fetch skipped: fatal L0 failure")
            page = await fetcher.fetch(page_source)
            page_source = getattr(fetcher, "sources", {}).get(page_source, page_source)
        except Exception as e:  # noqa: BLE001
            engine_notes.append(f"target page fetch ({cfg.fetch.provider}) failed: {type(e).__name__}: {e}")
            if cfg.fetch.provider != "builtin" and not l0.fatal_failures:
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
        TRACE.log("target_page", url=l0.final_url or l0.normalized_url, chars=len(inv.target_page_text or ""), text=inv.target_page_text,
                  injection=next((c.model_dump() for c in l0.checks if c.name == "injection"), None))

        fp = cfg.package_registry_fast_path
        if not l0.fatal_failures and fp.enabled and fp.mode != "full" and l0.platform in ("pypi", "npm"):
            await progress.report(f"registry fast path ({l0.platform})", 0.12)
            try:
                fast = await registry.run(l0, t0, trace_id, req.project)
            except Exception as e:  # noqa: BLE001
                fast = None
                engine_notes.append(f"registry fast path error: {type(e).__name__}: {e}")
            if fast is not None:
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
                                   trace_id=trace_id, duration_s=round(time.time() - t0, 1))
                return res

        if l0.fatal_failures:
            # No need to spend LLM budget: deterministic failure is final.
            from .models import LLMSubmission
            sub = LLMSubmission(identity=IdentityGraph(product=req.project))
            engine_notes.append("L1 skipped: fatal L0 failure")
        else:
            await progress.report("L1: identity investigation (LLM + tools)", 0.15)
            try:
                sub = await inv.investigate(req.project, req.url, req.description, l0, cached_identity)
            except Exception as e:  # noqa: BLE001
                from .models import LLMSubmission
                sub = LLMSubmission(identity=IdentityGraph(product=req.project))
                engine_notes.append(f"investigation failed: {type(e).__name__}: {e}")
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
        TRACE.log("aging_result", ages=ages, target_domain_age=target_age)
        prov = None
        registry_state = None
        if not l0.fatal_failures and l0.platform in ("pypi", "npm") and l0.platform_owner:
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
            TRACE.log("provenance", provenance=prov, registry_state={k: v for k, v in (registry_state or {}).items() if k != "signals"})
        await progress.report("rules engine: verifying evidence and deciding", 0.88)
        dec = decide(cfg, l0, sub, inv.evidence_store, req.project, cached_identity, ages, target_age, prov, registry_state)
        from .devtools.capture import capture
        capture(trace_id, req.project, req.url, cfg, l0, sub, inv.evidence_store, cached_identity, ages, target_age, prov, registry_state, dec)
        engine_notes.extend(dec.notes)
        TRACE.log("rules_decision", verdict=dec.verdict.value, confidence=dec.confidence, notes=dec.notes,
                  established_domains=dec.established_domains, established_orgs=dec.established_orgs,
                  evidence=[e.model_dump() for e in dec.evidence], submission=sub.model_dump())
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
            }, cfg.cache.identity_ttl_hours * 3600)

        await progress.report(f"decision {dec.verdict.value}; writing reason", 0.92)
        reason = await _write_reason(llm, req, dec.verdict, dec.confidence, engine_notes, l0, sub)
        await progress.report("done", 1.0)
        result = VerifyResult(
            verdict=dec.verdict, confidence=round(dec.confidence, 2), reason=reason,
            evidence=dec.evidence, checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
            identity=sub.identity, risk_signals=l0.risk_signals + sub.risk_notes, cache_hits=sorted(set(cache_hits)),
            engine_notes=engine_notes, trace_id=trace_id, duration_s=round(time.time() - t0, 1),
            path="l0_fatal" if l0.fatal_failures else "full",
        )
    finally:
        await fetcher.close()
        await search.close()
        await structured.close()
    return result


async def _write_reason(llm: LLM, req: VerifyRequest, verdict: Verdict, confidence: float, notes: list[str], l0, sub) -> str:
    findings = "\n".join(f"- {n}" for n in notes)
    findings += "\n" + "\n".join(f"- check {c.name}: {c.status}{' (' + c.message + ')' if c.message else ''}" for c in l0.checks)
    prompt = reason_prompt().format(project=req.project, url=req.url, description=req.description, verdict=verdict.value,
                                  confidence=confidence, findings=findings, narrative=sub.identity.narrative or sub.proposed_reason)
    try:
        msg = await llm.chat([{"role": "user", "content": prompt}], temperature=0.2)
        text = (msg.content or "").strip()
        import re
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if text:
            return text
    except Exception:  # noqa: BLE001
        pass
    return f"{verdict.value}: " + "; ".join(notes[:6])
