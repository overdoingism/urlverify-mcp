"""Fixed, gap-driven lookups that run BEFORE the LLM (AGENTS.md §6.3).

Everything here is mechanical: which lookups run depends only on the target type and on what is still missing, and
which Wikidata entity is accepted is decided by exact rules. The result is a deterministic submission (records cited
by fact id) that the rules engine judges exactly like LLM evidence. If that already decides the case, the LLM is not
started; otherwise the LLM is told what is already known and which identity edges are still missing.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..checks.urltools import etld1_of, host_of
from ..config import Config
from ..evidence import EvidenceStore
from ..models import Evidence, IdentityGraph, L0Result, LLMSubmission

MAX_WIKIMEDIA_QUERIES = 3
MAX_EXTRA_OWNER_RECORDS = 2
_PLATFORM_FACT = re.compile(r"\.(owner|login|name|full_name|id|author|blog|homepage|website|is_verified|verification_method)$")
_PLATFORM_HOST = {"github": "github.com", "huggingface": "huggingface.co", "gitlab": "gitlab.com"}


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def entity_accepted(ent: dict[str, Any], name: str, l0: L0Result) -> str | None:
    """Why a Wikidata search hit is accepted as the project / developer, or None. Exact rules only: the label or an
    alias equals the searched name, or the entity's official website / source repository points at the target."""
    n = _norm(name)
    if n and (_norm(ent.get("label") or "") == n or any(_norm(a) == n for a in ent.get("aliases") or [])):
        return "label or alias equals the searched name"
    sites = {etld1_of(host_of(u if "://" in u else "https://" + u)) for u in ent.get("official_website") or [] if isinstance(u, str)}
    if l0.etld1 and l0.etld1 in sites and not (l0.platform_scope == "user_content"):
        return f"official website is the target domain {l0.etld1}"
    host = _PLATFORM_HOST.get(l0.platform or "")
    if host and l0.platform_owner:
        prefix = f"{host}/{l0.platform_owner.lower()}"
        if any(str(r).lower() == prefix or str(r).lower().startswith(prefix + "/") for r in ent.get("official_repos") or []):
            return f"source repository is under the target owner {l0.platform_owner}"
    return None


class Prefetch:
    def __init__(self, cfg: Config, structured, store: EvidenceStore):
        self.cfg = cfg
        self.structured = structured
        self.store = store
        self.notes: list[str] = []
        self.codes: list[str] = []
        self.wikimedia_queries = 0
        self.searched: set[str] = set()
        self.evidence: list[Evidence] = []
        self.domains: list[str] = []
        self.orgs: dict[str, list[str]] = {}
        self.developers: list[str] = []

    # ------------------------------------------------------------------ helpers
    def _cite(self, source: str, kind: str, claim: str) -> None:
        # Wikimedia records are curated identity data: all their facts are cited. Platform records are cited only for
        # who they belong to and what they link to; other fields (a fork's parent, stars ...) are not identity claims.
        fids = [f for f, (src, path, _) in self.store.facts.items()
                if src == source and (kind in ("wikidata", "wikipedia") or _PLATFORM_FACT.search(path))]
        if fids:
            self.evidence.append(Evidence(kind=kind, source=source, tier=1 if kind in ("wikidata", "wikipedia") else 2,
                                          claim=f"[fixed lookup] {claim}", facts=fids, supports=True))

    def _add_domain(self, url: str) -> None:
        d = etld1_of(host_of(url if "://" in url else "https://" + url))
        if d and d not in self.domains:
            self.domains.append(d)

    def _add_org(self, platform: str, owner: str) -> None:
        lst = self.orgs.setdefault(platform, [])
        if owner and owner.lower() not in [o.lower() for o in lst]:
            lst.append(owner)

    def _record(self, tool: str, args: dict, raw: dict, kind: str, source: str) -> None:
        from ..agent.loop import _record_aliases
        self.store.record(source, json.dumps(raw, ensure_ascii=False), kind, aliases=_record_aliases(tool, args, raw))

    # ------------------------------------------------------------------ lookups
    async def wikimedia(self, name: str, l0: L0Result) -> bool:
        """One Wikidata search for `name`; every accepted entity (and its enwiki article) becomes a cited record."""
        key = _norm(name)
        if not key or key in self.searched or self.wikimedia_queries >= MAX_WIKIMEDIA_QUERIES:
            return False
        self.searched.add(key)
        self.wikimedia_queries += 1
        ic = self.cfg.identity
        r = await self.structured.wikidata(name, ic.history_days, ic.min_stable_revisions)
        if not r.get("ok"):
            self.notes.append(f"fixed lookup: Wikidata unavailable for '{name}' ({r.get('error')})")
            return False
        accepted = False
        for ent in r.get("entities") or []:
            why = entity_accepted(ent, name, l0)
            if not why:
                continue
            accepted = True
            src = ent["source"]
            self._record("wikidata_lookup", {"name": name}, ent, "wikidata", src)
            self._cite(src, "wikidata", f"Wikidata {ent.get('qid')} '{ent.get('label')}' ({why})")
            self.notes.append(f"fixed lookup: Wikidata '{name}' -> {ent.get('qid')} '{ent.get('label')}' accepted: {why}")
            for u in ent.get("official_website") or []:
                self._add_domain(u)
            for rep in ent.get("official_repos") or []:
                parts = str(rep).lower().split("/")
                plat = next((p for p, h in _PLATFORM_HOST.items() if h == parts[0]), None)
                if plat and len(parts) >= 2:
                    self._add_org(plat, parts[1])
            for dev in ent.get("developer") or []:
                for u in dev.get("official_website") or []:
                    self._add_domain(u)
                if dev.get("label") and dev["label"] not in self.developers:
                    self.developers.append(dev["label"])
            title = ent.get("enwiki")
            if title:
                w = await self.structured.wikipedia_history(title, ic.history_days, ic.min_stable_revisions)
                if w.get("ok") and w.get("found") and w.get("source"):
                    self._record("wikipedia_history", {"title": title}, w, "wikipedia", w["source"])
                    self._cite(w["source"], "wikipedia", f"Wikipedia '{w.get('title')}' (sitelink of {ent.get('qid')})")
        if not accepted:
            self.notes.append(f"fixed lookup: Wikidata '{name}': no entity matched by label, alias or link to the target")
        return accepted

    async def platform_record(self, platform: str, owner: str, repo: str | None) -> None:
        if platform == "github":
            r = await self.structured.github(owner, repo)
        elif platform == "huggingface":
            r = await self.structured.huggingface(owner, repo)
        elif platform == "flathub":
            r = await self.structured.flathub(owner)
            if r.get("found") is False:
                self.codes.append("FLATHUB_APP_NOT_FOUND")
                self.notes.append(f"fixed lookup: Flathub has no app {owner}")
                return
            if r.get("ok") and not (r.get("owner_info") or {}).get("is_verified"):
                self.codes.append("FLATHUB_UNVERIFIED")
                self.notes.append(f"fixed lookup: Flathub app {owner} is not verified by its developer (community packaging)")
        else:
            return
        tool = {"github": "github_info", "huggingface": "huggingface_info", "flathub": "flathub_info"}[platform]
        if r.get("ok") and r.get("source"):
            self._record(tool, {"owner": owner, "repo": repo}, r, platform, r["source"])
            self._cite(r["source"], platform, f"{platform} record of {owner}{'/' + repo if repo else ''}")
            self.notes.append(f"fixed lookup: {platform} record of {owner}{'/' + repo if repo else ''}")
        else:
            self.notes.append(f"fixed lookup: {platform} record of {owner} unavailable ({r.get('error')})")

    async def homebrew(self, l0: L0Result, project: str) -> None:
        """Website targets: casks that download from the target domain (reverse lookup in Homebrew's catalogue).
        The cask record's token / homepage / url lines are the evidence (brew.sh family)."""
        from ..source.resolve import _json_snippets
        from .brew_index import BrewCaskIndex
        idx = BrewCaskIndex(self.cfg.storage.resolved(), self.cfg.net.user_agent,
                            refresh_days=self.cfg.identity.homebrew_index_refresh_days)
        for c in await idx.casks_for(l0.etld1, project):
            api = f"https://formulae.brew.sh/api/cask/{c['token']}.json"
            try:
                r = await self.structured.client.get(api)
            except Exception as e:  # noqa: BLE001
                self.notes.append(f"fixed lookup: Homebrew cask {c['token']} unavailable ({type(e).__name__})")
                continue
            if r.status_code != 200:
                continue
            doc, text = r.json(), r.text
            urls = [doc.get("url")] + [(v or {}).get("url") for v in (doc.get("variations") or {}).values()]
            on_target = next((u for u in urls if isinstance(u, str) and etld1_of(host_of(u)) == l0.etld1), None)
            parts = _json_snippets(text, [("token", doc.get("token")), ("homepage", doc.get("homepage")), ("url", on_target)])
            m = re.search(r'"name"\s*:\s*\[[^\]]*\]', text)
            if m:
                parts.insert(1, m.group(0))
            if len(parts) < 2:
                continue
            self.store[api] = text
            self.evidence.append(Evidence(kind="distro", source=api, tier=1, supports=True, quote=" ... ".join(parts),
                                          claim=f"[fixed lookup] Homebrew cask {c['token']} downloads from {l0.etld1}"))
            self.notes.append(f"fixed lookup: Homebrew cask '{c['token']}' downloads from {l0.etld1}")

    # ------------------------------------------------------------------ plan
    async def run(self, l0: L0Result, project: str) -> LLMSubmission:
        """The fixed plan: Wikimedia for the project name, then the package / repository name, then the developer of
        the first accepted entity (at most MAX_WIKIMEDIA_QUERIES searches); the target owner's platform record; and
        the platform records of owners that a stable Wikimedia repository record names."""
        names = [project]
        if l0.platform in ("pypi", "npm", "nuget") and l0.platform_owner:
            names.append(l0.platform_owner.split("/")[-1])
        elif l0.platform_repo:
            names.append(l0.platform_repo)
        hit = False
        for n in names:
            hit = await self.wikimedia(n, l0) or hit
            if hit:
                break
        for dev in list(self.developers):
            if self.wikimedia_queries >= MAX_WIKIMEDIA_QUERIES:
                break
            await self.wikimedia(dev, l0)
        if not hit:
            self.codes.append("WIKIMEDIA_NO_MATCH")
        if l0.platform in ("github", "huggingface", "flathub") and l0.platform_scope == "user_content" and l0.platform_owner:
            self._add_org(l0.platform, l0.platform_owner)
            await self.platform_record(l0.platform, l0.platform_owner, l0.platform_repo)
            extra = [o for o in self.orgs.get(l0.platform, []) if o.lower() != l0.platform_owner.lower()][:MAX_EXTRA_OWNER_RECORDS]
            for o in extra:
                await self.platform_record(l0.platform, o, None)
        if l0.platform_scope != "user_content" and l0.etld1:
            self._add_domain(l0.etld1)
            if self.cfg.identity.homebrew_reverse_lookup:
                try:
                    await self.homebrew(l0, project)
                except Exception as e:  # noqa: BLE001
                    self.notes.append(f"fixed lookup: Homebrew reverse lookup failed ({type(e).__name__}: {e})")
        return self.submission(project)

    def submission(self, project: str) -> LLMSubmission:
        ident = IdentityGraph(product=project, developer=self.developers[0] if self.developers else None,
                              official_domains=list(self.domains), official_orgs={k: list(v) for k, v in self.orgs.items()},
                              narrative="fixed lookups (no LLM): " + "; ".join(self.notes)[:1500])
        return LLMSubmission(identity=ident, evidence=list(self.evidence), proposed_verdict="UNVERIFIABLE")


def merge(det: LLMSubmission, llm: LLMSubmission) -> LLMSubmission:
    """LLM findings added to the fixed-lookup submission: evidence and candidate domains / orgs are unioned; the LLM's
    prose (developer, aliases, narrative, proposal) is kept."""
    ident = llm.identity.model_copy(deep=True)
    for d in det.identity.official_domains:
        if d not in ident.official_domains:
            ident.official_domains.append(d)
    for p, orgs in det.identity.official_orgs.items():
        lst = ident.official_orgs.setdefault(p, [])
        for o in orgs:
            if o.lower() not in [x.lower() for x in lst]:
                lst.append(o)
    ident.developer = ident.developer or det.identity.developer
    seen = {(e.source, tuple(e.facts)) for e in llm.evidence}
    evidence = list(llm.evidence) + [e for e in det.evidence if (e.source, tuple(e.facts)) not in seen]
    return LLMSubmission(identity=ident, evidence=evidence, proposed_verdict=llm.proposed_verdict,
                         proposed_reason=llm.proposed_reason, risk_notes=llm.risk_notes)


def brief(det_decision, store: EvidenceStore) -> str:
    """What the LLM is told before it starts: records already fetched (facts citable by id) and the missing edges."""
    lines = ["Fixed lookups already ran (do not repeat them; cite their facts by id if they support something):"]
    for src, rid in store.record_ids.items():
        lines.append(f"- {rid} {store.kinds.get(src)} {src}")
    if det_decision.established_edges:
        lines.append("Already established: " + ", ".join(det_decision.established_edges))
    if det_decision.missing_edges:
        lines.append("STILL MISSING (focus on these):")
        for m in det_decision.missing_edges:
            extra = f" ({m['have']} of {m['need']} independent sources)" if "need" in m else (f" ({m['why']})" if m.get("why") else "")
            lines.append(f"- {m['edge']}{extra}")
        lines.append("Independent sources that count: reputable media, distribution / package-manager manifests, a different platform "
                     "than the one already cited. The target's own pages never count.")
    return "\n".join(lines)
