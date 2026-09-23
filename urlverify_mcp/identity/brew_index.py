"""Reverse lookup: which Homebrew casks download from a given domain (AGENTS.md §6.3, fixed lookups for websites).

Homebrew publishes its whole cask catalogue (formulae.brew.sh/api/cask.json, ~2 MB compressed). It is fetched on
first use, reduced to a small domain index in state/, and refreshed at most every `refresh_days` with a conditional
request (ETag): an unchanged catalogue costs one 304. The per-cask record is fetched separately so the evidence can be
quoted verbatim.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from ..checks.urltools import etld1_of, host_of
from ..health import observe

CATALOGUE = "https://formulae.brew.sh/api/cask.json"


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def build_index(casks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """download-URL domain -> [{token, name, homepage}] (every variation's URL counts)."""
    idx: dict[str, list[dict[str, Any]]] = {}
    for c in casks:
        urls = [c.get("url")] + [(v or {}).get("url") for v in (c.get("variations") or {}).values()]
        doms = {etld1_of(host_of(u)) for u in urls if isinstance(u, str) and u.startswith("http")}
        entry = {"token": c.get("token"), "name": (c.get("name") or [])[:3], "homepage": c.get("homepage")}
        for d in doms:
            if d:
                idx.setdefault(d, []).append(entry)
    return idx


def accepted(entry: dict[str, Any], project: str, etld1: str) -> bool:
    """A cask downloading from the target domain is taken as evidence only if it is about the project (token or a
    name equals it) or its homepage is on the same domain. Exact rules; no fuzzy matching."""
    p = _norm(project)
    if p and (_norm(entry.get("token") or "") == p or any(_norm(n) == p for n in entry.get("name") or [])):
        return True
    hp = entry.get("homepage") or ""
    return bool(hp) and etld1_of(host_of(hp)) == etld1


class BrewCaskIndex:
    def __init__(self, state_dir: str | Path, user_agent: str, timeout: float = 30, refresh_days: int = 7):
        self.path = Path(state_dir) / "brew_cask_index.json"
        self.ua, self.timeout, self.refresh_s = user_agent, timeout, refresh_days * 86400

    def _load(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def index(self) -> dict[str, list[dict[str, Any]]] | None:
        cached = self._load()
        if cached and time.time() - cached.get("fetched_at", 0) < self.refresh_s:
            return cached.get("by_domain")
        headers = {"User-Agent": self.ua, "Accept-Encoding": "gzip"}
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
                r = await c.get(CATALOGUE, headers=headers)
            if r.status_code == 304 and cached:
                cached["fetched_at"] = time.time()
                self._save(cached)
                observe("homebrew", True)
                return cached.get("by_domain")
            r.raise_for_status()
            by_domain = build_index(r.json())
            observe("homebrew", True)
        except Exception as e:  # noqa: BLE001
            observe("homebrew", False, f"{type(e).__name__}: {e}")
            return cached.get("by_domain") if cached else None     # stale index is better than none
        self._save({"fetched_at": time.time(), "etag": r.headers.get("etag"), "by_domain": by_domain})
        return by_domain

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    async def casks_for(self, etld1: str, project: str, limit: int = 3) -> list[dict[str, Any]]:
        idx = await self.index()
        if not idx:
            return []
        return [e for e in idx.get(etld1, []) if accepted(e, project, etld1)][:limit]
