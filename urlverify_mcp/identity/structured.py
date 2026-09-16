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

WIKI_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
URL_RE = re.compile(r"https?://[^\s|}\]<>\"']+", re.I)


class Structured:
    def __init__(self, timeout: float, user_agent: str, github_token: str = ""):
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self.client = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)
        self.gh_headers = {"Authorization": f"Bearer {github_token}"} if github_token else {}

    async def close(self):
        await self.client.aclose()

    async def _json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        r = await self.client.get(url, params=params, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        return r.json()

    # ---------------- Wikidata
    async def wikidata(self, name: str, history_days: int, min_stable: int) -> dict[str, Any]:
        """Search entity, read P856 (official website), P178 (developer), P1448/P1813 (names), P8687?, and check P856 stability."""
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
                stability = await self._wikidata_stability(qid, history_days, min_stable) if official else None
                out_entities.append({
                    "qid": qid, "label": e.get("labels", {}).get("en", {}).get("value"),
                    "description": e.get("descriptions", {}).get("en", {}).get("value"),
                    "official_website": official, "developer": dev_labels,
                    "aliases": [a["value"] for a in e.get("aliases", {}).get("en", [])] if e.get("aliases") else [],
                    "enwiki": (e.get("sitelinks", {}).get("enwiki", {}) or {}).get("title"),
                    "stability": stability,
                    "source": f"https://www.wikidata.org/wiki/{qid}",
                })
            return {"ok": True, "found": True, "entities": out_entities}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def _wikidata_stability(self, qid: str, history_days: int, min_stable: int) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = await self._json(WIKIDATA_API, {"action": "query", "prop": "revisions", "titles": qid, "rvprop": "ids|timestamp|content",
                                                "rvslots": "main", "rvlimit": 20, "rvdir": "older", "format": "json", "formatversion": 2})
            revs = r["query"]["pages"][0].get("revisions", [])
            values = []
            for rev in revs:
                content = rev.get("slots", {}).get("main", {}).get("content", "")
                try:
                    j = json.loads(content)
                    v = [c["mainsnak"]["datavalue"]["value"] for c in j.get("claims", {}).get("P856", []) if "datavalue" in c.get("mainsnak", {})]
                except Exception:
                    v = []
                values.append({"ts": rev["timestamp"], "official": sorted(v)})
            # revision ≥ history_days old
            old = await self._json(WIKIDATA_API, {"action": "query", "prop": "revisions", "titles": qid, "rvprop": "ids|timestamp|content",
                                                  "rvslots": "main", "rvlimit": 1, "rvstart": cutoff, "rvdir": "older", "format": "json", "formatversion": 2})
            old_revs = old["query"]["pages"][0].get("revisions", [])
            old_val = None
            if old_revs:
                content = old_revs[0].get("slots", {}).get("main", {}).get("content", "")
                try:
                    j = json.loads(content)
                    old_val = sorted(c["mainsnak"]["datavalue"]["value"] for c in j.get("claims", {}).get("P856", []) if "datavalue" in c.get("mainsnak", {}))
                except Exception:
                    old_val = None
            return _stability_verdict(values, old_val, min_stable, history_days)
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    # ---------------- Wikipedia (infobox website + revision history)
    async def wikipedia_history(self, title: str, history_days: int, min_stable: int, lang: str = "en") -> dict[str, Any]:
        api = WIKI_API.format(lang=lang)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=history_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = await self._json(api, {"action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp|content", "rvslots": "main",
                                       "rvlimit": 15, "rvdir": "older", "redirects": 1, "format": "json", "formatversion": 2})
            page = r["query"]["pages"][0]
            if page.get("missing"):
                return {"ok": True, "found": False, "source": f"https://{lang}.wikipedia.org/wiki/{title}"}
            revs = page.get("revisions", [])
            values = [{"ts": rev["timestamp"], "official": _infobox_sites(rev.get("slots", {}).get("main", {}).get("content", ""))} for rev in revs]
            old = await self._json(api, {"action": "query", "prop": "revisions", "titles": page["title"], "rvprop": "ids|timestamp|content", "rvslots": "main",
                                         "rvlimit": 1, "rvstart": cutoff, "rvdir": "older", "format": "json", "formatversion": 2})
            old_revs = old["query"]["pages"][0].get("revisions", [])
            old_val = _infobox_sites(old_revs[0].get("slots", {}).get("main", {}).get("content", "")) if old_revs else None
            current = values[0]["official"] if values else []
            latest_text = revs[0].get("slots", {}).get("main", {}).get("content", "") if revs else ""
            devs = re.findall(r"\|\s*(?:developer|author|publisher|company)\s*=\s*([^\n|]+)", latest_text, flags=re.I)
            return {"ok": True, "found": True, "title": page["title"], "official_website": current,
                    "developer_fields": [re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", d).strip() for d in devs][:5],
                    "stability": _stability_verdict(values, old_val, min_stable, history_days),
                    "source": f"https://{lang}.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    # ---------------- Wayback
    async def wayback_first_seen(self, domain: str) -> dict[str, Any]:
        try:
            r = None
            last_exc = None
            for attempt in range(5):          # archive.org throttles with 503 / slow reads; back off 2s, 4s, 6s, 8s
                try:
                    r = await self.client.get("https://web.archive.org/cdx/search/cdx",
                                              params={"url": domain, "limit": 1, "fl": "timestamp,original", "filter": "statuscode:200", "output": "json"})
                except httpx.TimeoutException as e:
                    last_exc = e
                    r = None
                if r is not None and r.status_code < 500 and r.status_code != 429:
                    break
                await asyncio.sleep(2.0 * (attempt + 1))
            if r is None:
                return {"ok": False, "error": f"timeout after retries: {type(last_exc).__name__}"}
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}"}
            rows = r.json()
            if len(rows) < 2:
                return {"ok": True, "found": False, "domain": domain, "source": f"https://web.archive.org/web/*/{domain}"}
            ts = rows[1][0]
            first = datetime.strptime(ts[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
            return {"ok": True, "found": True, "domain": domain, "first_snapshot": first.date().isoformat(),
                    "age_days": (datetime.now(timezone.utc) - first).days, "source": f"https://web.archive.org/web/{ts}/{rows[1][1]}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    # ---------------- GitHub
    async def github(self, owner: str, repo: str | None = None) -> dict[str, Any]:
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
    async def huggingface(self, owner: str, repo: str | None = None) -> dict[str, Any]:
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
    async def pypi(self, name: str) -> dict[str, Any]:
        try:
            j = await self._json(f"https://pypi.org/pypi/{name}/json")
            info = j.get("info", {})
            return {"ok": True, "found": True, "name": info.get("name"), "home_page": info.get("home_page"),
                    "project_urls": info.get("project_urls"), "author": info.get("author"), "source": f"https://pypi.org/project/{name}/"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    async def npm(self, name: str) -> dict[str, Any]:
        try:
            j = await self._json(f"https://registry.npmjs.org/{name}")
            repo = j.get("repository", {})
            return {"ok": True, "found": True, "name": j.get("name"), "homepage": j.get("homepage"),
                    "repository": repo.get("url") if isinstance(repo, dict) else repo, "source": f"https://www.npmjs.com/package/{name}"}
        except Exception as ex:  # noqa: BLE001
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


def _infobox_sites(wikitext: str) -> list[str]:
    # only the infobox "website"/"homepage" field; strip cite templates so citation URLs on the same line are ignored
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", " ", wikitext or "", flags=re.S | re.I)
    text = re.sub(r"\{\{\s*cite[^{}]*\}\}", " ", text, flags=re.I)
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
