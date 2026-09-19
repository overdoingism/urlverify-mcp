"""Structured, machine-readable identity sources with temporal-stability checks.
All calls are best-effort: network errors return {"ok": False, "error": ...}."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..checks.urltools import etld1_of, host_of
from ..health import observe

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
URL_RE = re.compile(r"https?://[^\s|}\]<>\"']+", re.I)


def _obs(dep: str, r: dict) -> dict:
    observe(dep, r.get("ok") is not False, r.get("error"))
    return r


class Structured:
    def __init__(self, timeout: float, user_agent: str, github_token: str = ""):
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.client = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)
        self._wayback_down = 0
        self.gh_headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}

    async def close(self):
        await self.client.aclose()

    # ---------------- observed-health wrappers
    async def wikidata(self, *a, **k): return _obs("wikidata", await self._wikidata(*a, **k))
    async def wikipedia_history(self, *a, **k): return _obs("wikipedia", await self._wikipedia_history(*a, **k))
    async def wayback_first_seen(self, *a, **k): return _obs("wayback", await self._wayback_first_seen(*a, **k))
    async def github(self, *a, **k): return _obs("github", await self._github(*a, **k))
    async def huggingface(self, *a, **k): return _obs("huggingface", await self._huggingface(*a, **k))
    async def pypi(self, *a, **k): return _obs("pypi", await self._pypi(*a, **k))
    async def npm(self, *a, **k): return _obs("npm", await self._npm(*a, **k))
    async def nuget(self, *a, **k): return _obs("nuget", await self._nuget(*a, **k))

    async def _json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        r = await self.client.get(url, params=params, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        return r.json()

    # ---------------- Wikidata
    async def _wikidata(self, name: str, history_days: int, min_stable: int) -> dict[str, Any]:
        """Search entity, read P856 (official website), P1324 (source code repository), P178 (developer), names/aliases,
        and check P856 / P1324 revision-history stability."""
        try:
            s = await self._json(WIKIDATA_API, {"action": "wbsearchentities", "search": name, "language": "en", "format": "json", "limit": 5})
            hits = s.get("search", [])
            if not hits:
                return {"ok": True, "found": False, "source": f"{WIKIDATA_API}?action=wbsearchentities&search={name}"}
            out_entities = []
            for h in hits[:3]:
                qid = h["id"]
                ent = await self._json(WIKIDATA_API, {"action": "wbgetentities", "ids": qid, "props": "labels|descriptions|claims|sitelinks", "languages": "en", "format": "json"})
                e = ent["entities"][qid]
                claims = e.get("claims", {})
                def _vals(pid):
                    vals = []
                    for c in claims.get(pid, []):
                        dv = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                        if isinstance(dv, dict) and "id" in dv:
                            vals.append(dv["id"])
                        elif isinstance(dv, dict) and "text" in dv:
                            vals.append(dv["text"])
                        elif isinstance(dv, str):
                            vals.append(dv)
                    return vals
                official = _vals("P856")
                repos = [r for r in (_norm_repo(v) for v in _vals("P1324")) if r]
                dev_ids = _vals("P178") + _vals("P123") + _vals("P176")  # developer, publisher, manufacturer
                dev_labels = []
                if dev_ids:
                    d = await self._json(WIKIDATA_API, {"action": "wbgetentities", "ids": "|".join(dev_ids[:5]), "props": "labels|claims", "languages": "en", "format": "json"})
                    for did, de in d.get("entities", {}).items():
                        lab = de.get("labels", {}).get("en", {}).get("value")
                        dweb = []
                        for c in de.get("claims", {}).get("P856", []):
                            v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                            if isinstance(v, str):
                                dweb.append(v)
                        dev_labels.append({"id": did, "label": lab, "official_website": dweb})
                stab = await self._wikidata_stability(qid, history_days, min_stable, [p for p, v in (("P856", official), ("P1324", repos)) if v])
                out_entities.append({
                    "qid": qid, "label": e.get("labels", {}).get("en", {}).get("value"),
                    "description": e.get("descriptions", {}).get("en", {}).get("value"),
                    "official_website": official, "official_repos": repos, "developer": dev_labels,
                    "aliases": [a["value"] for a in e.get("aliases", {}).get("en", [])] if e.get("aliases") else [],
                    "enwiki": (e.get("sitelinks", {}).get("enwiki", {}) or {}).get("title"),
                    "stability": stab.get("P856"), "repo_stability": stab.get("P1324"),
                    "source": f"https://www.wikidata.org/wiki/{qid}",
                })
            return {"ok": True, "found": True, "entities": out_entities}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def _wikidata_stability(self, qid: str, history_days: int, min_stable: int, pids: list[str]) -> dict[str, Any]:
        """Revision-history stability of the given properties (P856 official website, P1324 source repository), one fetch."""
        if not pids:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def _claim_vals(content: str, pid: str) -> list[str] | None:
            try:
                j = json.loads(content)
            except Exception:
                return None
            vals = [c["mainsnak"]["datavalue"]["value"] for c in j.get("claims", {}).get(pid, []) if "datavalue" in c.get("mainsnak", {})]
            if pid == "P1324":
                vals = [r for r in (_norm_repo(v) for v in vals if isinstance(v, str)) if r]
            return sorted(v for v in vals if isinstance(v, str))
        try:
            r = await self._json(WIKIDATA_API, {"action": "query", "prop": "revisions", "titles": qid, "rvprop": "ids|timestamp|content",
                                                "rvslots": "main", "rvlimit": 20, "rvdir": "older", "format": "json", "formatversion": 2})
            revs = r["query"]["pages"][0].get("revisions", [])
            # revision ≥ history_days old
            old = await self._json(WIKIDATA_API, {"action": "query", "prop": "revisions", "titles": qid, "rvprop": "ids|timestamp|content",
                                                  "rvslots": "main", "rvlimit": 1, "rvstart": cutoff, "rvdir": "older", "format": "json", "formatversion": 2})
            old_revs = old["query"]["pages"][0].get("revisions", [])
            old_content = old_revs[0].get("slots", {}).get("main", {}).get("content", "") if old_revs else None
            out: dict[str, Any] = {}
            for pid in pids:
                values = [{"ts": rev["timestamp"], "official": _claim_vals(rev.get("slots", {}).get("main", {}).get("content", ""), pid) or []} for rev in revs]
                old_val = _claim_vals(old_content, pid) if old_content is not None else None
                out[pid] = _stability_verdict(values, old_val, min_stable, history_days)
            return out
        except Exception as ex:  # noqa: BLE001
            return {pid: {"ok": False, "error": f"{type(ex).__name__}: {ex}"} for pid in pids}

    # ---------------- Wikipedia (infobox website + revision history)
    async def _wikipedia_history(self, title: str, history_days: int, min_stable: int, lang: str = "en") -> dict[str, Any]:
        api = WIKI_API.format(lang=lang)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = await self._json(api, {"action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp|content", "rvslots": "main",
                                       "rvlimit": 15, "rvdir": "older", "redirects": 1, "format": "json", "formatversion": 2})
            page = r["query"]["pages"][0]
            if page.get("missing"):
                return {"ok": True, "found": False, "source": f"https://{lang}.wikipedia.org/wiki/{title}"}
            revs = page.get("revisions", [])
            contents = [rev.get("slots", {}).get("main", {}).get("content", "") for rev in revs]
            values = [{"ts": rev["timestamp"], "official": _infobox_sites(c)} for rev, c in zip(revs, contents)]
            repo_values = [{"ts": rev["timestamp"], "official": _infobox_repos(c)} for rev, c in zip(revs, contents)]
            old = await self._json(api, {"action": "query", "prop": "revisions", "titles": page["title"], "rvprop": "ids|timestamp|content", "rvslots": "main",
                                         "rvlimit": 1, "rvstart": cutoff, "rvdir": "older", "format": "json", "formatversion": 2})
            old_revs = old["query"]["pages"][0].get("revisions", [])
            old_content = old_revs[0].get("slots", {}).get("main", {}).get("content", "") if old_revs else None
            old_val = _infobox_sites(old_content) if old_content is not None else None
            old_repo = _infobox_repos(old_content) if old_content is not None else None
            current = values[0]["official"] if values else []
            current_repos = repo_values[0]["official"] if repo_values else []
            latest_text = revs[0].get("slots", {}).get("main", {}).get("content", "") if revs else ""
            devs = re.findall(r"\|\s*(?:developer|author|publisher|company)\s*=\s*([^\n|]+)", latest_text, flags=re.I)
            return {"ok": True, "found": True, "title": page["title"], "official_website": current, "official_repos": current_repos,
                    "developer_fields": [re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", d).strip() for d in devs][:5],
                    "stability": _stability_verdict(values, old_val, min_stable, history_days),
                    "repo_stability": _stability_verdict(repo_values, old_repo, min_stable, history_days) if current_repos else None,
                    "source": f"https://{lang}.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    # ---------------- Wayback
    async def _wayback_first_seen(self, domain: str) -> dict[str, Any]:
        """Earliest capture of a domain / URL. The availability API (cached, rarely throttled) is asked for the capture
        closest to 1996 on a couple of host variants; CDX is only a fallback and is tried at most twice. After two
        consecutive 503s within this Structured instance (one verification) Wayback is not asked again."""
        if self._wayback_down >= 2:
            return {"ok": False, "error": "wayback skipped for the rest of this verification (repeated 503)"}
        target = domain.strip()
        bare = re.sub(r"^https?://", "", target).rstrip("/")
        variants = [bare]
        if "/" not in bare:
            variants.append(("www." + bare) if not bare.startswith("www.") else bare[4:])
        best = None
        for v in variants:
            try:
                r = await self.client.get("https://archive.org/wayback/available", params={"url": v, "timestamp": "19960101"})
                if r.status_code >= 500 or r.status_code == 429:
                    self._wayback_down += 1
                    continue
                self._wayback_down = 0
                snap = (r.json().get("archived_snapshots") or {}).get("closest")
                if snap and snap.get("timestamp"):
                    ts = snap["timestamp"][:8]
                    if best is None or ts < best[0]:
                        best = (ts, snap.get("url"))
            except Exception as ex:  # noqa: BLE001
                self._wayback_down += 1
                last = f"{type(ex).__name__}: {ex}"
        if best:
            first = datetime.strptime(best[0], "%Y%m%d").replace(tzinfo=timezone.utc)
            return {"ok": True, "found": True, "domain": domain, "first_snapshot": first.date().isoformat(),
                    "age_days": (datetime.now(timezone.utc) - first).days, "source": best[1] or f"https://web.archive.org/web/*/{bare}", "via": "availability"}
        # CDX fallback: at most 2 attempts, no status filter
        for attempt in range(2):
            if self._wayback_down >= 2:
                break
            try:
                r = await self.client.get("https://web.archive.org/cdx/search/cdx",
                                          params={"url": bare, "limit": 1, "fl": "timestamp,original", "output": "json"})
            except httpx.TimeoutException as e:
                self._wayback_down += 1
                last = f"{type(e).__name__}"
                continue
            if r.status_code >= 500 or r.status_code == 429:
                self._wayback_down += 1
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(2.0)
                continue
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            rows = r.json()
            if len(rows) < 2:
                return {"ok": True, "found": False, "domain": domain, "source": f"https://web.archive.org/web/*/{bare}"}
            ts = rows[1][0]
            first = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return {"ok": True, "found": True, "domain": domain, "first_snapshot": first.date().isoformat(),
                    "age_days": (datetime.now(timezone.utc) - first).days, "source": f"https://web.archive.org/web/{ts}/{rows[1][1]}", "via": "cdx"}
        return {"ok": False, "error": f"wayback unavailable ({locals().get('last', 'no capture')})"}

    # ---------------- GitHub
    async def _github(self, owner: str, repo: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "owner": owner}
        try:
            o = await self._json(f"https://api.github.com/users/{owner}", headers=self.gh_headers)
            out["owner_info"] = {k: o.get(k) for k in ("login", "type", "name", "blog", "company", "created_at", "public_repos", "followers", "html_url")}
            if o.get("type") == "Organization":
                try:
                    org = await self._json(f"https://api.github.com/orgs/{owner}", headers=self.gh_headers)
                    out["owner_info"]["is_verified"] = org.get("is_verified")
                    out["owner_info"]["blog"] = org.get("blog") or out["owner_info"]["blog"]
                except Exception:
                    pass
            out["source"] = o.get("html_url")
            if repo:
                r = await self._json(f"https://api.github.com/repos/{owner}/{repo}", headers=self.gh_headers)
                out["repo_info"] = {k: r.get(k) for k in ("full_name", "fork", "created_at", "pushed_at", "stargazers_count", "forks_count", "homepage", "archived", "html_url")}
                if r.get("fork") and r.get("parent"):
                    out["repo_info"]["parent"] = r["parent"].get("full_name")
                out["source"] = r.get("html_url")
            return out
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "owner": owner}

    # ---------------- Hugging Face
    async def _huggingface(self, owner: str, repo: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "owner": owner}
        try:
            try:
                org = await self._json(f"https://huggingface.co/api/organizations/{owner}/overview")
                out["owner_info"] = {"type": "org", "name": org.get("name"), "fullname": org.get("fullname"), "is_verified": org.get("isVerified"),
                                     "is_enterprise": org.get("isEnterprise"), "num_models": org.get("numModels")}
            except Exception:
                u = await self._json(f"https://huggingface.co/api/users/{owner}/overview")
                out["owner_info"] = {"type": "user", "name": u.get("user"), "fullname": u.get("fullname"), "is_pro": u.get("isPro"), "num_models": u.get("numModels")}
            out["source"] = f"https://huggingface.co/{owner}"
            if repo:
                m = None
                for kind in ("models", "datasets", "spaces"):
                    try:
                        m = await self._json(f"https://huggingface.co/api/{kind}/{owner}/{repo}")
                        out["repo_kind"] = kind
                        break
                    except Exception:
                        continue
                if m:
                    out["repo_info"] = {k: m.get(k) for k in ("id", "author", "createdAt", "lastModified", "downloads", "likes", "gated", "private")}
                    out["source"] = f"https://huggingface.co/{m.get('id')}"
            return out
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}", "owner": owner}

    # ---------------- package registries
    async def _nuget(self, name: str) -> dict[str, Any]:
        from .releases import ReleaseMetadata, ReleaseTarget
        try:
            api = ReleaseMetadata(self.client)
            meta = await api.resolve(ReleaseTarget("nuget", name))
            entry = meta.get("catalog_entry")
            entry = await api.json(entry) if isinstance(entry, str) else entry
            if not isinstance(entry, dict):
                raise ValueError("NuGet catalog entry unavailable")
            return {"ok": True, "found": True, "name": entry.get("id"), "version": meta["version"],
                    "project_url": entry.get("projectUrl"), "authors": entry.get("authors"),
                    "description": entry.get("description"), "published": meta.get("published_at"),
                    "listed": meta.get("listed"), "source": meta["source"]}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def _pypi(self, name: str) -> dict[str, Any]:
        try:
            j = await self._json(f"https://pypi.org/pypi/{name}/json")
            info = j.get("info", {})
            return {"ok": True, "found": True, "name": info.get("name"), "home_page": info.get("home_page"),
                    "project_urls": info.get("project_urls"), "author": info.get("author"), "source": f"https://pypi.org/project/{name}/"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def _npm(self, name: str) -> dict[str, Any]:
        try:
            j = await self._json(f"https://registry.npmjs.org/{name}/latest")   # small document; the full one lists every version
            repo = j.get("repository", {})
            return {"ok": True, "found": True, "name": j.get("name"), "version": j.get("version"), "homepage": j.get("homepage"),
                    "repository": repo.get("url") if isinstance(repo, dict) else repo, "source": f"https://www.npmjs.com/package/{name}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


def _strip_refs(wikitext: str) -> str:
    """Remove <ref .../> (self-closing first, so it can never swallow text up to a later </ref>), <ref>...</ref> and
    {{cite ...}} templates, so citation URLs on an infobox line are ignored."""
    text = re.sub(r"<ref\b[^>]*/\s*>", " ", wikitext or "", flags=re.I)
    text = re.sub(r"<ref\b[^>]*>.*?</ref\s*>", " ", text, flags=re.S | re.I)
    return re.sub(r"\{\{\s*cite[^{}]*\}\}", " ", text, flags=re.I)


def _norm_repo(url: str) -> str | None:
    """'https://GitHub.com/Org/Repo.git/' -> 'github.com/org/repo' (host + path, lower-case, no scheme/www/.git)."""
    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    h = host_of(u)
    if not h:
        return None
    path = u.split("://", 1)[1].split("/", 1)[1] if "/" in u.split("://", 1)[1] else ""
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    h = h.lower()[4:] if h.lower().startswith("www.") else h.lower()
    return f"{h}/{path.lower()}" if path else h


def _infobox_repos(wikitext: str) -> list[str]:
    # infobox "repo"/"repository" field only; cite templates stripped so citation URLs on the same line are ignored
    text = _strip_refs(wikitext)
    repos = set()
    for line in re.findall(r"^\s*\|\s*(?:repo|repository)\s*=\s*([^\n]+)", text, flags=re.I | re.M):
        for u in URL_RE.findall(line):
            r = _norm_repo(u)
            if r:
                repos.add(r)
        for u in re.findall(r"\{\{\s*URL\s*\|\s*([^|}]+)", line, flags=re.I):
            r = _norm_repo(u.strip())
            if r:
                repos.add(r)
    return sorted(repos)


def _infobox_sites(wikitext: str) -> list[str]:
    # only the infobox "website"/"homepage" field; strip cite templates so citation URLs on the same line are ignored
    text = _strip_refs(wikitext)
    m = re.findall(r"^\s*\|\s*(?:website|homepage)\s*=\s*([^\n]+)", text, flags=re.I | re.M)
    sites = set()
    for line in m:
        for u in URL_RE.findall(line):
            sites.add(etld1_of(host_of(u)))
        for u in re.findall(r"\{\{\s*URL\s*\|\s*([^|}]+)", line, flags=re.I):
            u = u.strip()
            sites.add(etld1_of(host_of(u if "://" in u else "https://" + u)))
    return sorted(s for s in sites if s)


def _stability_verdict(values: list[dict], old_val, min_stable: int, history_days: int) -> dict[str, Any]:
    current = values[0]["official"] if values else []
    consistent = sum(1 for v in values if v["official"] == current)
    stable = consistent >= min_stable and (old_val is None or old_val == current)
    recent_change = old_val is not None and old_val != current
    return {"ok": True, "current": current, "value_days_ago": old_val, "consistent_recent_revisions": consistent,
            "revisions_checked": len(values), "stable": stable, "recent_change": recent_change,
            "history_days": history_days, "min_stable_revisions": min_stable}
