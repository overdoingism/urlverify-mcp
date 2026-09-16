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
from .structured import Structured

PYPI_TOP_URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
BUNDLED_PYPI = Path(__file__).parent.parent / "data" / "pypi_top.json"
NPM_BULK = "https://api.npmjs.org/downloads/point/last-week/"


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


def _variants(name: str) -> set[str]:
    """Near-names an attacker would register: one-edit variants plus separator / common-confusion swaps."""
    out: set[str] = set()
    n = name.lower()
    alphabet = "abcdefghijklmnopqrstuvwxyz-_."
    for i in range(len(n)):
        out.add(n[:i] + n[i + 1:])                                  # deletion
        for c in alphabet:
            out.add(n[:i] + c + n[i + 1:])                          # substitution
    for i in range(len(n) - 1):
        out.add(n[:i] + n[i + 1] + n[i] + n[i + 2:])               # transposition
    for i in range(len(n) + 1):
        for c in alphabet:
            out.add(n[:i] + c + n[i:])                              # insertion
    for a, b in (("-", "_"), ("_", "-"), ("-", ""), ("_", ""), ("py", ""), ("js", ""), ("s", ""), ("", "s"), ("", "js"), ("", "-js")):
        if a:
            out.add(n.replace(a, b))
        else:
            out.add(n + b)
    out.discard(n)
    return {v for v in out if v and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", v)}


class RegistryFastPath:
    def __init__(self, cfg: Config, structured: Structured):
        self.cfg = cfg
        self.structured = structured
        self.client = structured.client

    # ---------------- popularity data
    async def pypi_top(self) -> dict[str, int]:
        cache = Path(os.path.expanduser("~/.urlverify_mcp/pypi_top.json"))
        ttl = self.cfg.registry_fast_path.toplist_refresh_days * 86400
        if cache.is_file() and time.time() - cache.stat().st_mtime < ttl:
            try:
                return {r[0]: r[1] for r in json.loads(cache.read_text(encoding="utf-8"))["rows"]}
            except Exception:  # noqa: BLE001
                pass
        try:
            r = await self.client.get(PYPI_TOP_URL)
            if r.status_code == 200:
                rows = [[x["project"], int(x["download_count"])] for x in r.json()["rows"][:5000]]
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"rows": rows, "fetched": time.time()}), encoding="utf-8")
                return {a: b for a, b in rows}
        except Exception:  # noqa: BLE001
            pass
        return {r[0]: r[1] for r in json.loads(BUNDLED_PYPI.read_text(encoding="utf-8"))["rows"]}

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
        sig.update({"exists": True, "canonical": info.get("name"), "releases": len(j.get("releases") or {}),
                    "age_days": _age_days(min(uploads)) if uploads else None, "latest_age_days": _age_days(max(uploads)) if uploads else None,
                    "project_urls": info.get("project_urls") or {}, "home_page": info.get("home_page"), "summary": info.get("summary"),
                    "source": f"https://pypi.org/project/{info.get('name') or name}/"})
        top = await self.pypi_top()
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
        repo = j.get("repository")
        sig.update({"exists": True, "canonical": j.get("name"), "releases": len(versions), "age_days": _age_days(times.get("created")),
                    "latest_age_days": _age_days(times.get("modified")), "homepage": j.get("homepage"),
                    "repository": repo.get("url") if isinstance(repo, dict) else repo, "maintainers": len(j.get("maintainers") or []),
                    "source": f"https://www.npmjs.com/package/{name}"})
        # popularity of the candidate and of its near-names (bulk endpoint, unscoped names only)
        if name.startswith("@"):
            sig["downloads_week"] = None
            sig["typosquat_hits"] = None      # scoped packages: bulk API unsupported -> unknown
            return sig
        variants = list(_variants(name))[:127]
        try:
            mine = (await self.client.get(NPM_BULK + name)).json().get("downloads")
            sig["downloads_week"] = mine
            hits = []
            for i in range(0, len(variants), 128):
                chunk = variants[i:i + 128]
                data = (await self.client.get(NPM_BULK + ",".join(chunk))).json()
                for k, v in (data or {}).items():
                    if isinstance(v, dict) and v.get("downloads") and v["downloads"] > max((mine or 0) * 20, 100_000):
                        hits.append({"popular": k, "downloads": v["downloads"]})
            sig["typosquat_hits"] = sorted(hits, key=lambda h: -h["downloads"])[:3]
        except Exception as e:  # noqa: BLE001
            sig["downloads_week"] = None
            sig["typosquat_hits"] = None
            sig["error"] = f"{type(e).__name__}: {e}"
        return sig

    async def repo_signal(self, sig: dict[str, Any]) -> dict[str, Any]:
        """Linked GitHub repo: exists, not a fork, not brand new."""
        urls = list((sig.get("project_urls") or {}).values()) + [sig.get("home_page"), sig.get("homepage"), sig.get("repository")]
        for u in urls:
            if not u:
                continue
            m = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)", str(u))
            if m:
                gh = await self.structured.github(m.group(1), m.group(2))
                if gh.get("ok") and gh.get("repo_info"):
                    ri = gh["repo_info"]
                    return {"repo": ri.get("full_name"), "fork": ri.get("fork"), "age_days": _age_days(ri.get("created_at")),
                            "stars": ri.get("stargazers_count"), "archived": ri.get("archived"), "source": ri.get("html_url"), "ok": True}
                return {"repo": f"{m.group(1)}/{m.group(2)}", "ok": False, "error": gh.get("error", "not found")}
        return {"repo": None, "ok": None}

    # ---------------- decision
    async def run(self, l0: L0Result, t0: float, trace_id: str) -> VerifyResult | None:
        fp = self.cfg.registry_fast_path
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
        repo = await self.repo_signal(sig)
        hits = sig.get("typosquat_hits")
        age, rel = sig.get("age_days"), sig.get("releases") or 0
        popular = bool(sig.get("downloads_rank_month")) or ((sig.get("downloads_week") or 0) >= 100_000)

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
        if repo.get("ok") and repo.get("fork"):
            notes.append(f"linked repository {repo['repo']} is a fork")
        if repo.get("ok") and (repo.get("age_days") or 0) < fp.min_age_days and not popular:
            notes.append(f"linked repository is only {repo.get('age_days')} days old")
        if repo.get("ok") is None and not popular:
            notes.append("no linked source repository and not a widely downloaded package")

        if notes or unknowns:
            TRACE.log("registry_fast_path", signals=sig, repo=repo, outcome="inconclusive", notes=notes + unknowns)
            return None
        why = [f"{l0.platform} package '{sig.get('canonical') or name}' exists for {age} days with {rel} releases",
               "no more-popular near-name package (typosquat check clean)"]
        if popular:
            why.append("widely downloaded" + (f" (rank in monthly top list)" if sig.get("downloads_rank_month") else f" ({sig.get('downloads_week'):,}/week)"))
        if repo.get("ok"):
            why.append(f"linked repository {repo['repo']} is {repo.get('age_days')} days old, not a fork, {repo.get('stars')} stars")
        TRACE.log("registry_fast_path", signals=sig, repo=repo, outcome="verified")
        return self._result(Verdict.TRUE, fp.confidence, l0, sig, repo, why, t0, trace_id)

    def _result(self, verdict: Verdict, conf: float, l0: L0Result, sig: dict, repo: dict, why: list[str], t0: float, trace_id: str) -> VerifyResult:
        ev = [Evidence(kind="package_registry", source=sig.get("source") or l0.normalized_url, tier=1, claim="; ".join(why[:2]),
                       quote=json.dumps({k: sig.get(k) for k in ("canonical", "age_days", "releases", "downloads_rank_month", "downloads_week")}),
                       verified_quote=True, notes=["structured registry data"])]
        if repo.get("ok"):
            ev.append(Evidence(kind="github", source=repo["source"], tier=2, claim=f"linked repository {repo['repo']}",
                               quote=json.dumps({k: repo.get(k) for k in ("fork", "age_days", "stars")}), verified_quote=True, notes=["structured GitHub data"]))
        reason = (f"{verdict.value}: registry fast path. " + " ".join(w[0].upper() + w[1:] + "." for w in why)
                  + " Deterministic checks (TLS, DNS, redirects) passed. No LLM was involved; pass options.mode='full' for the complete investigation.")
        return VerifyResult(verdict=verdict, confidence=conf, reason=reason, evidence=ev,
                            checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
                            identity=IdentityGraph(product=sig.get("canonical") or l0.platform_owner or "", official_orgs={l0.platform: [l0.platform_owner]},
                                                   official_repos=[repo["repo"]] if repo.get("repo") else []),
                            risk_signals=l0.risk_signals, engine_notes=[f"registry fast path: {w}" for w in why], trace_id=trace_id,
                            duration_s=round(time.time() - t0, 1), path="registry_fast_path")
