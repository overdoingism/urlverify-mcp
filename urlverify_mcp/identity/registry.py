"""Registry fast path (PyPI / npm): decide "is this the real package or a look-alike?" from structured registry
data alone, in seconds, without the LLM. Every signal is best-effort; any unknown or suspicious signal makes the
fast path *inconclusive*, which hands the case to the full pipeline. The fast path can only speed things up,
never decide in the wrong direction."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..checks.urltools import levenshtein
from ..config import Config
from ..models import Evidence, IdentityGraph, L0Result, Verdict, VerifyResult
from ..tracelog import TRACE
from .provenance import Provenance
from .structured import Structured



def norm_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _age_days(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def is_security_holding(npm_doc: dict) -> bool:
    """npm's own statement that a name is not a real package: after a malicious package is removed, the npm security
    team re-publishes the name as a 'security holding package' at version 0.0.1-security."""
    latest = (npm_doc.get("dist-tags") or {}).get("latest")
    desc = str(npm_doc.get("description") or "").lower()
    return latest == "0.0.1-security" or "security holding package" in desc


def pypi_latest_all_yanked(pypi_doc: dict) -> bool:
    """PyPI: every file of the latest release withdrawn (yanked) — the registry's signal that the release is not to be used."""
    info = pypi_doc.get("info") or {}
    files = (pypi_doc.get("releases") or {}).get(info.get("version")) or []
    return bool(files) and all(f.get("yanked") for f in files)


def _names_match(project: str, package: str) -> bool:
    """Caller's project name vs package name: equal after normalisation, or one contains the other (\"LM Studio\" / lmstudio)."""
    a = re.sub(r"[^a-z0-9]", "", project.lower())
    b = re.sub(r"[^a-z0-9]", "", package.lower())
    return bool(a and b) and (a == b or a in b or b in a)


def _manifest_names(filename: str, text: str) -> list[str]:
    """Package names declared by a manifest file (best effort, parser first, regex fallback)."""
    names: list[str] = []
    try:
        if filename == "pyproject.toml":
            import tomllib
            d = tomllib.loads(text)
            for path in (("project", "name"), ("tool", "poetry", "name"), ("tool", "flit", "metadata", "module"), ("tool", "setuptools", "name")):
                cur: Any = d
                for k in path:
                    cur = cur.get(k) if isinstance(cur, dict) else None
                if isinstance(cur, str):
                    names.append(cur)
        elif filename == "setup.cfg":
            import configparser
            cp = configparser.ConfigParser()
            cp.read_string(text)
            if cp.has_option("metadata", "name"):
                names.append(cp.get("metadata", "name"))
        elif filename == "setup.py":
            names += re.findall(r"""\bname\s*=\s*["']([A-Za-z0-9_.-]+)["']""", text)
        elif filename == "package.json":
            j = json.loads(text)
            if isinstance(j.get("name"), str):
                names.append(j["name"])
    except Exception:  # noqa: BLE001  (unparsable manifest: fall back to a loose regex)
        names += re.findall(r"""\bname\s*[=:]\s*["']([A-Za-z0-9_.@/-]+)["']""", text)
    return names


class RegistryFastPath:
    def __init__(self, cfg: Config, structured: Structured):
        self.cfg = cfg
        self.structured = structured
        self.client = structured.client

    # ---------------- popularity data
    async def pypi_top(self) -> dict[str, int] | None:
        """Top-N PyPI packages by monthly downloads. Downloaded on first use only, streamed and closed after N rows
        (the file is sorted by downloads), re-validated at most every toplist_refresh_days with If-None-Match so an
        unchanged list costs a 304 and no body. Returns None when nothing is available (signal unknown)."""
        fp = self.cfg.package_registry_fast_path
        cache = self.cfg.storage.resolved() / "pypi_top.json"
        cached: dict[str, Any] | None = None
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                cached = None
        fresh_enough = cached and time.time() - float(cached.get("fetched", 0)) < fp.toplist_refresh_days * 86400 and len(cached.get("rows", [])) >= min(fp.toplist_size, 100)
        if fresh_enough:
            return {r[0]: r[1] for r in cached["rows"][: fp.toplist_size]}
        headers = {"Accept": "application/json"}
        if cached and cached.get("etag") and len(cached.get("rows", [])) >= fp.toplist_size:
            headers["If-None-Match"] = cached["etag"]
        try:
            rows: list[list] = []
            async with self.client.stream("GET", fp.toplist_url, headers=headers) as r:
                if r.status_code == 304 and cached:
                    cached["fetched"] = time.time()
                    cache.write_text(json.dumps(cached), encoding="utf-8")
                    return {x[0]: x[1] for x in cached["rows"][: fp.toplist_size]}
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                etag = r.headers.get("etag")
                buf = ""
                async for chunk in r.aiter_text():
                    buf += chunk
                    for m in re.finditer(r'\{"download_count":\s*(\d+),\s*"project":\s*"([^"]+)"\}', buf):
                        rows.append([m.group(2), int(m.group(1))])
                    if rows:
                        buf = buf[buf.rfind("}") + 1:]         # keep only the unfinished tail
                    if len(rows) >= fp.toplist_size:
                        break                                    # closes the connection; the remainder is never downloaded
            rows = rows[: fp.toplist_size]
            if not rows:
                raise RuntimeError("no rows parsed")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"rows": rows, "fetched": time.time(), "etag": etag, "url": fp.toplist_url}), encoding="utf-8")
            return {a: b for a, b in rows}
        except Exception as e:  # noqa: BLE001
            TRACE.log("pypi_toplist", error=f"{type(e).__name__}: {e}", cached_rows=len(cached["rows"]) if cached else 0)
            if cached and cached.get("rows"):
                return {r[0]: r[1] for r in cached["rows"][: fp.toplist_size]}   # stale beats nothing
            return None

    # ---------------- signals
    async def pypi_signals(self, name: str) -> dict[str, Any]:
        sig: dict[str, Any] = {"registry": "pypi", "name": name}
        try:
            j = (await self.client.get(f"https://pypi.org/pypi/{name}/json")).json()
        except Exception as e:  # noqa: BLE001
            return {**sig, "exists": None, "error": f"{type(e).__name__}: {e}"}
        info = j.get("info") or {}
        if not info:
            return {**sig, "exists": False}
        uploads = [f["upload_time_iso_8601"] for files in (j.get("releases") or {}).values() for f in files if f.get("upload_time_iso_8601")]
        latest = info.get("version")
        files = (j.get("releases") or {}).get(latest) or []
        sig["latest_yanked"] = pypi_latest_all_yanked(j)
        sig.update({"exists": True, "canonical": info.get("name"), "releases": len(j.get("releases") or {}), "version": latest,
                    "filename": next((f["filename"] for f in files if f.get("filename", "").endswith(".whl")), files[0]["filename"] if files else None),
                    "age_days": _age_days(min(uploads)) if uploads else None, "latest_age_days": _age_days(max(uploads)) if uploads else None,
                    "project_urls": info.get("project_urls") or {}, "home_page": info.get("home_page"), "summary": info.get("summary"),
                    "source": f"https://pypi.org/project/{info.get('name') or name}/"})
        if not self.cfg.package_registry_fast_path.typosquat_check:
            sig["typosquat_hits"] = []
            sig["typosquat_check"] = "disabled by config"
            return sig
        top = await self.pypi_top()
        if top is None:
            sig["typosquat_hits"] = None          # unknown: fast path will be inconclusive
            return sig
        n = norm_pypi(name)
        top_norm = {norm_pypi(k): v for k, v in top.items()}
        sig["downloads_rank_month"] = top_norm.get(n)
        # typosquat: a much more popular package within one edit (two for long names)
        hits = []
        for other, dl in top_norm.items():
            if other == n:
                continue
            lim = 1 if len(other) < 8 else 2
            if abs(len(other) - len(n)) <= lim and 0 < levenshtein(n, other) <= lim:
                mine = top_norm.get(n, 0)
                if dl > max(mine * 20, 100_000):
                    hits.append({"popular": other, "downloads": dl})
        sig["typosquat_hits"] = sorted(hits, key=lambda h: -h["downloads"])[:3]
        return sig

    async def npm_signals(self, name: str) -> dict[str, Any]:
        sig: dict[str, Any] = {"registry": "npm", "name": name}
        try:
            r = await self.client.get(f"https://registry.npmjs.org/{name}")
            if r.status_code == 404:
                return {**sig, "exists": False}
            j = r.json()
        except Exception as e:  # noqa: BLE001
            return {**sig, "exists": None, "error": f"{type(e).__name__}: {e}"}
        times = j.get("time") or {}
        versions = [v for v in times if v not in ("created", "modified")]
        sig["security_holding"] = is_security_holding(j)
        repo = j.get("repository")
        sig.update({"exists": True, "canonical": j.get("name"), "releases": len(versions), "age_days": _age_days(times.get("created")),
                    "version": (j.get("dist-tags") or {}).get("latest"),
                    "latest_age_days": _age_days(times.get("modified")), "homepage": j.get("homepage"),
                    "repository": repo.get("url") if isinstance(repo, dict) else repo, "maintainers": len(j.get("maintainers") or []),
                    "source": f"https://www.npmjs.com/package/{name}"})
        # No near-name comparison for npm: there is no popularity reference to compare against, and generating
        # "likely typos" is guesswork (people do not see their own typos). Provenance gates TRUE instead.
        sig["typosquat_hits"] = []
        sig["typosquat_check"] = "not applicable to npm (no popularity reference); provenance required"
        return sig

    async def repo_signal(self, sig: dict[str, Any]) -> dict[str, Any]:
        """Linked GitHub repo, checked in both directions: the registry metadata points at the repo AND the repo's own
        manifest (pyproject.toml / setup.cfg / setup.py / package.json) declares this package name. A one-way claim
        from either side is not enough; a typosquatter can point at anyone's repo, and a repo can be anyone's."""
        urls = list((sig.get("project_urls") or {}).values()) + [sig.get("home_page"), sig.get("homepage"), sig.get("repository")]
        for u in urls:
            if not u:
                continue
            m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)", str(u))
            if not m:
                continue
            owner, repo = m.group(1), m.group(2)
            gh = await self.structured.github(owner, repo)
            if not (gh.get("ok") and gh.get("repo_info")):
                return {"repo": f"{owner}/{repo}", "ok": False, "error": gh.get("error", "not found")}
            ri = gh["repo_info"]
            back = await self._manifest_declares(owner, repo, sig["registry"], sig.get("canonical") or sig["name"])
            return {"repo": ri.get("full_name"), "fork": ri.get("fork"), "age_days": _age_days(ri.get("created_at")),
                    "stars": ri.get("stargazers_count"), "archived": ri.get("archived"), "source": ri.get("html_url"), "ok": True,
                    "bidirectional": back["declared"], "manifest": back.get("file"), "manifest_error": back.get("error")}
        return {"repo": None, "ok": None}

    async def _manifest_declares(self, owner: str, repo: str, registry: str, pkg: str) -> dict[str, Any]:
        """Does the repository's manifest declare this package name? Reads raw files from the default branch and parses
        them properly (tomllib / configparser / json); a different name in *every* manifest found means "no"."""
        files = ("pyproject.toml", "setup.cfg", "setup.py") if registry == "pypi" else ("package.json",)
        want = norm_pypi(pkg) if registry == "pypi" else pkg.lower()
        found: list[str] = []
        last_err = None
        for f in files:
            try:
                r = await self.client.get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{f}")
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                continue
            if r.status_code != 200:
                continue
            found.append(f)
            for n in _manifest_names(f, r.text[:200_000]):
                if (norm_pypi(n) if registry == "pypi" else n.lower()) == want:
                    return {"declared": True, "file": f}
        if found:
            return {"declared": False, "file": ", ".join(found)}
        return {"declared": None, "error": last_err or "no manifest found on default branch"}

    # ---------------- decision
    async def run(self, l0: L0Result, t0: float, trace_id: str, project: str = "") -> VerifyResult | None:
        fp = self.cfg.package_registry_fast_path
        if l0.platform not in ("pypi", "npm") or not l0.platform_owner:
            return None
        name = l0.platform_owner
        sig = await (self.pypi_signals(name) if l0.platform == "pypi" else self.npm_signals(name))
        notes: list[str] = []
        if sig.get("exists") is None:
            notes.append(f"registry API unavailable ({sig.get('error')}); fast path inconclusive")
            TRACE.log("registry_fast_path", signals=sig, outcome="inconclusive")
            return None
        if sig.get("exists") is False:
            TRACE.log("registry_fast_path", signals=sig, outcome="not_found")
            return self._result(Verdict.FALSE, 0.9, l0, sig, {}, [f"package '{name}' does not exist on {l0.platform}"], t0, trace_id)
        if sig.get("security_holding"):
            # the registry's own verdict on the name; no investigation can overturn it
            TRACE.log("registry_fast_path", signals=sig, outcome="security_holding")
            l0.risk_signals.append("npm_security_holding_package")
            return self._result(Verdict.FALSE, 0.95, l0, sig, {},
                                [f"'{name}' is an npm security holding package (version 0.0.1-security): the name was taken over by the npm "
                                 "security team after a malicious package was removed; nothing legitimate is published under it"], t0, trace_id)
        if sig.get("latest_yanked"):
            l0.risk_signals.append("pypi_latest_release_yanked")
        # only now the caller's project name: the facts above are about the target itself
        if fp.require_project_match and project and not _names_match(project, name):
            TRACE.log("registry_fast_path", outcome="inconclusive", note=f"project '{project}' does not match package '{name}'")
            l0.risk_signals.append(f"project_package_mismatch:{project}!={name}")
            return None
        repo = await self.repo_signal(sig)
        prov = await self.provenance_signal(sig)
        hits = sig.get("typosquat_hits")
        age, rel = sig.get("age_days"), sig.get("releases") or 0
        if hits:
            notes.append(f"name is one edit away from a far more popular package: {hits[0]['popular']} ({hits[0]['downloads']:,} downloads)")
            l0.risk_signals.append(f"registry_typosquat:{hits[0]['popular']}")
            TRACE.log("registry_fast_path", signals=sig, repo=repo, outcome="suspicious")
            return None                                   # let the full pipeline judge, with the hint attached
        unknowns = []
        if hits is None:
            unknowns.append("near-name popularity unknown")
        if age is None:
            unknowns.append("package age unknown")
        if age is not None and age < fp.min_age_days:
            notes.append(f"package is only {age} days old (need {fp.min_age_days})")
        if rel < fp.min_releases:
            notes.append(f"only {rel} release(s) (need {fp.min_releases})")
        if repo.get("ok") is False:
            notes.append(f"linked repository {repo.get('repo')} not reachable: {repo.get('error')}")
        elif repo.get("ok") is None:
            notes.append("no linked source repository in the registry metadata")
        else:
            if repo.get("fork"):
                notes.append(f"linked repository {repo['repo']} is a fork")
            if repo.get("bidirectional") is False:
                notes.append(f"repository {repo['repo']} manifest ({repo.get('manifest')}) declares a different package name")
            elif repo.get("bidirectional") is None:
                unknowns.append(f"could not read the repository manifest ({repo.get('manifest_error')})")

        # provenance (the registry's signed statement of which repository's CI published this version) is required:
        # metadata links alone cannot say WHO published, and a new/small package deserves the full investigation
        if not prov.get("found"):
            notes.append("no build provenance (npm attestation / PyPI PEP 740) for the latest version")
        else:
            powner = prov["repo"][0].lower()
            if repo.get("ok") and repo.get("repo") and repo["repo"].lower().split("/")[0] != powner:
                notes.append(f"provenance repository {prov['repo_url']} differs from the metadata repository {repo['repo']}")
            if sig.get("scope") and sig["scope"].lower() != powner:
                notes.append(f"npm scope '@{sig['scope']}' does not match the provenance repository owner '{powner}'")
            gh = await self.structured.github(prov["repo"][0])
            oi = (gh.get("owner_info") or {}) if gh.get("ok") else {}
            if not oi.get("is_verified"):
                notes.append(f"provenance repository owner '{powner}' is not a domain-verified GitHub organisation")
            else:
                prov["owner_verified"] = True
                prov["owner_blog"] = oi.get("blog")
                if fp.require_project_match and project and not (_names_match(project, powner) or _names_match(project, name)):
                    notes.append(f"project '{project}' matches neither the package nor the provenance owner")
        if notes or unknowns:
            TRACE.log("registry_fast_path", signals=sig, repo=repo, provenance=prov, outcome="inconclusive", notes=notes + unknowns)
            return None
        why = [f"{l0.platform} package '{sig.get('canonical') or name}' exists for {age} days with {rel} releases",
               ("no more-popular near-name package on the PyPI popularity list" if l0.platform == "pypi" else "npm: no near-name reference; provenance required instead"),
               f"registry metadata points at {repo['repo']} and its {repo.get('manifest')} declares this package (bidirectional link); "
               f"repository is not a fork, {repo.get('age_days')} days old, {repo.get('stars')} stars",
               f"build provenance ({prov.get('kind')}) signed for version {sig.get('version')}: published by CI of {prov['repo_url']}"
               + (f" ({prov['workflow']})" if prov.get("workflow") else "") + f"; owner '{prov['repo'][0]}' is a domain-verified GitHub organisation"
               + (f" ({prov.get('owner_blog')})" if prov.get("owner_blog") else "")
               + ("; independently verified by deps.dev" if prov.get("depsdev_verified") else "")]
        TRACE.log("registry_fast_path", signals=sig, repo=repo, provenance=prov, outcome="verified")
        return self._result(Verdict.TRUE, fp.confidence, l0, sig, repo, why, t0, trace_id, prov)

    async def registry_state(self, l0: L0Result) -> dict[str, Any] | None:
        """The registry's own statement about the target name, independent of any path or mode:
        {"state": "missing"|"security_holding"|"latest_yanked"|"ok", ...}."""
        if l0.platform not in ("pypi", "npm") or not l0.platform_owner:
            return None
        sig = await (self.pypi_signals(l0.platform_owner) if l0.platform == "pypi" else self.npm_signals(l0.platform_owner))
        if sig.get("exists") is None:
            return {"state": "unknown", "error": sig.get("error")}
        if sig.get("exists") is False:
            return {"state": "missing"}
        if sig.get("security_holding"):
            return {"state": "security_holding", "version": sig.get("version")}
        if sig.get("latest_yanked"):
            return {"state": "latest_yanked", "version": sig.get("version")}
        return {"state": "ok", "version": sig.get("version"), "signals": sig}

    async def provenance_signal(self, sig: dict[str, Any]) -> dict[str, Any]:
        pv = Provenance(self.client)
        name = sig.get("canonical") or sig["name"]
        if sig["registry"] == "npm":
            prov = await pv.npm(name, sig.get("version"))
        else:
            prov = await pv.pypi(name, sig.get("version"), sig.get("filename"))
        if prov.get("found"):
            dd = await pv.depsdev(sig["registry"], name, sig.get("version"))
            prov["depsdev_verified"] = bool(dd.get("found") and dd.get("verified") and dd["repo"][0].lower() == prov["repo"][0].lower())
            prov["depsdev_source"] = dd.get("source")
        return prov

    def _result(self, verdict: Verdict, conf: float, l0: L0Result, sig: dict, repo: dict, why: list[str], t0: float, trace_id: str,
                prov: dict | None = None) -> VerifyResult:
        ev = [Evidence(kind="package_registry", source=sig.get("source") or l0.normalized_url, tier=1, claim="; ".join(why[:2]),
                       quote=json.dumps({k: sig.get(k) for k in ("canonical", "age_days", "releases", "downloads_rank_month", "downloads_week")}),
                       verified_quote=True, notes=["structured registry data"])]
        if repo.get("ok"):
            ev.append(Evidence(kind="github", source=repo["source"], tier=2, claim=f"bidirectional link with repository {repo['repo']}",
                               quote=json.dumps({k: repo.get(k) for k in ("fork", "age_days", "stars", "bidirectional", "manifest")}), verified_quote=True, notes=["structured GitHub data + manifest"]))
        if prov and prov.get("found"):
            ev.append(Evidence(kind="provenance", source=prov.get("source") or "", tier=1, claim=f"signed build provenance from {prov.get('repo_url')}",
                               quote=json.dumps({k: prov.get(k) for k in ("repo_url", "workflow", "builder", "kind", "depsdev_verified")}),
                               verified_quote=True, notes=["registry-signed attestation" + ("; corroborated by deps.dev" if prov.get("depsdev_verified") else "")]))
        reason = (f"{verdict.value}: registry fast path. " + " ".join(w[0].upper() + w[1:] + "." for w in why)
                  + " Deterministic checks (TLS, DNS, redirects) passed. No LLM was involved; pass options.mode='full' for the complete investigation.")
        return VerifyResult(verdict=verdict, confidence=conf, reason=reason, evidence=ev,
                            checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
                            identity=IdentityGraph(product=sig.get("canonical") or l0.platform_owner or "", official_orgs={l0.platform: [l0.platform_owner]},
                                                   official_repos=[repo["repo"]] if repo.get("repo") else []),
                            risk_signals=l0.risk_signals, engine_notes=[f"registry fast path: {w}" for w in why], trace_id=trace_id,
                            duration_s=round(time.time() - t0, 1), path="registry_fast_path")
