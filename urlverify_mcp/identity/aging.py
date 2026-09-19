"""Temporal provenance of tier-3 sources ("has this post existed long enough to be trusted?").

Timestamps are never produced by the LLM. Each platform has a deterministic method; unknown domains fall back to the
Wayback Machine's first capture of that exact URL. Strength:
  strong  - platform API / snowflake id / Wayback first capture   -> can promote a tier-3 source on its own
  weak    - page metadata (JSON-LD / meta tags), self-reported     -> needs Wayback corroboration
  none    - login-walled or unarchivable platforms                  -> never promoted
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..providers.public_http import public_client

from ..checks.urltools import etld1_of, host_of
from ..health import observe
from .structured import Structured

DEFAULT_AGING_SOURCES: dict[str, str] = {
    "reddit.com": "reddit_api", "redd.it": "reddit_api",
    "news.ycombinator.com": "hn_api",
    "stackoverflow.com": "stackexchange_api", "stackexchange.com": "stackexchange_api", "superuser.com": "stackexchange_api",
    "serverfault.com": "stackexchange_api", "askubuntu.com": "stackexchange_api", "mathoverflow.net": "stackexchange_api",
    "x.com": "snowflake", "twitter.com": "snowflake",
    "github.com": "github_api", "githubusercontent.com": "github_api",
    "huggingface.co": "huggingface_api", "hf.co": "huggingface_api",
    "youtube.com": "jsonld", "medium.com": "jsonld", "dev.to": "jsonld", "substack.com": "jsonld", "hashnode.dev": "jsonld",
    # login-walled / unarchivable: never promoted
    "facebook.com": "none", "instagram.com": "none", "threads.net": "none", "discord.com": "none", "discord.gg": "none",
    "t.me": "none", "telegram.org": "none", "linkedin.com": "none", "whatsapp.com": "none",
}
METHOD_STRENGTH = {"reddit_api": "strong", "hn_api": "strong", "stackexchange_api": "strong", "snowflake": "strong",
                   "discourse": "strong", "github_api": "strong", "huggingface_api": "strong", "wayback": "strong", "jsonld": "weak", "none": "none"}
TWITTER_EPOCH_MS = 1288834974657
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(ts: float | None) -> int | None:
    return int((_now() - datetime.fromtimestamp(ts, tz=timezone.utc)).days) if ts else None


def method_for(url: str, aging_sources: dict[str, str]) -> str:
    host = host_of(url)
    e1 = etld1_of(host)
    for key in (host, e1):
        if key in aging_sources:
            return aging_sources[key]
    # Discourse detection is structural (any self-hosted forum): /t/<slug>/<id>
    if re.search(r"/t/[^/]+/\d+", url):
        return "discourse"
    return "wayback"


def snowflake_ts(status_id: int) -> float:
    return ((status_id >> 22) + TWITTER_EPOCH_MS) / 1000.0


def _result(method: str, url: str, **kw) -> dict[str, Any]:
    r = {"ok": True, "method": method, "strength": METHOD_STRENGTH.get(method, "none"), "url": url,
         "created_ts": None, "edited_ts": None, "age_days": None, "quality": {}, "api": None, "error": None}
    r.update(kw)
    if r["created_ts"] and r["age_days"] is None:
        r["age_days"] = _age(r["created_ts"])
    return r


class Aging:
    def __init__(self, timeout: float, user_agent: str, structured: Structured, aging_sources: dict[str, str] | None = None):
        self.client = public_client(timeout=timeout, headers={"User-Agent": user_agent, "Accept": "application/json, text/html"}, follow_redirects=True)
        self.structured = structured
        self._reddit_lock = asyncio.Lock()   # reddit rate-limits bursts; serialize with a pause
        self.sources = dict(DEFAULT_AGING_SOURCES)
        self.sources.update({k.lower(): v for k, v in (aging_sources or {}).items()})

    async def close(self):
        await self.client.aclose()

    async def age_of(self, url: str) -> dict[str, Any]:
        method = method_for(url, self.sources)
        try:
            if method == "none":
                return _result(method, url, ok=False, error="login-walled or unarchivable platform; cannot be dated")
            fn = getattr(self, f"_{method}")
            if method == "reddit_api":
                async with self._reddit_lock:
                    r = await fn(url)
                    await asyncio.sleep(2.0)
            else:
                r = await fn(url)
            if r.get("ok") and r["strength"] == "weak":
                # weak self-reported dates need Wayback corroboration
                wb = await self._wayback(url)
                if wb.get("ok") and wb.get("created_ts"):
                    r["corroborated_by_wayback"] = True
                    r["created_ts"] = max(r["created_ts"], wb["created_ts"]) if r.get("created_ts") else wb["created_ts"]
                    r["age_days"] = _age(r["created_ts"])
                    r["strength"] = "strong"
                else:
                    r["corroborated_by_wayback"] = False
            if method != "wayback":
                observe(f"aging:{method}", bool(r.get("ok")), r.get("error"))
            if r.get("ok") and not r.get("created_ts"):
                # method found nothing: fall back to Wayback
                wb = await self._wayback(url)
                if wb.get("created_ts"):
                    wb["note"] = f"{method} yielded no date; used Wayback"
                    return wb
                r["ok"] = False
                r["error"] = r.get("error") or "no timestamp available"
            return r
        except Exception as e:  # noqa: BLE001
            observe(f"aging:{method}", False, f"{type(e).__name__}: {e}")
            return _result(method, url, ok=False, error=f"{type(e).__name__}: {e}")

    # ---- methods
    async def _reddit_api(self, url: str) -> dict[str, Any]:
        m = re.search(r"/comments/([a-z0-9]{4,10})", url)
        if not m:
            return _result("reddit_api", url, ok=True, error="no post id in URL")
        pid = m.group(1)
        hdrs = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
        api = f"https://www.reddit.com/comments/{pid}.json"
        try:
            r = await self.client.get(api, params={"raw_json": 1}, headers=hdrs, follow_redirects=False)
            if r.status_code == 200:
                data = r.json()
                post = data[0]["data"]["children"][0]["data"]
                edited = post.get("edited")
                return _result("reddit_api", url, created_ts=float(post.get("created_utc") or 0) or None,
                               edited_ts=float(edited) if isinstance(edited, (int, float)) and edited else None, api=api,
                               quality={"score": post.get("score"), "num_comments": post.get("num_comments"), "subreddit": post.get("subreddit"),
                                        "author": post.get("author"), "removed": bool(post.get("removed_by_category"))})
        except Exception:  # noqa: BLE001
            pass
        # JSON blocked (login wall for some networks): Reddit's own RSS feed still carries <published>
        rss = f"https://www.reddit.com/comments/{pid}/.rss"
        r = await self.client.get(rss, headers={"User-Agent": BROWSER_UA, "Accept": "application/atom+xml, application/xml"}, follow_redirects=False)
        if r.status_code != 200:
            return _result("reddit_api", url, ok=True, error=f"reddit JSON and RSS unavailable (HTTP {r.status_code})", api=rss)
        m2 = re.search(r"<published>([^<]+)</published>", r.text)
        ts = _parse_dt(m2.group(1)) if m2 else None
        return _result("reddit_api", url, created_ts=ts, api=rss, quality={"via": "rss", "edited_unknown": True})

    async def _hn_api(self, url: str) -> dict[str, Any]:
        m = re.search(r"[?&]id=(\d+)", url)
        if not m:
            return _result("hn_api", url, ok=True, error="no item id")
        api = f"https://hn.algolia.com/api/v1/items/{m.group(1)}"
        j = (await self.client.get(api)).json()
        return _result("hn_api", url, created_ts=float(j.get("created_at_i") or 0) or None, api=api,
                       quality={"points": j.get("points"), "author": j.get("author"), "type": j.get("type")})

    async def _stackexchange_api(self, url: str) -> dict[str, Any]:
        host = host_of(url)
        site = host.replace("www.", "").split(".")[0] if not host.endswith("stackexchange.com") else host.replace("www.", "").split(".")[0]
        if host.replace("www.", "") == "stackexchange.com":
            return _result("stackexchange_api", url, ok=True, error="network root, no site")
        qa = re.search(r"/(questions|q|a|answers)/(\d+)", url)
        if not qa:
            return _result("stackexchange_api", url, ok=True, error="no question/answer id")
        kind, ident = qa.group(1), qa.group(2)
        endpoint = "answers" if kind in ("a", "answers") else "questions"
        api = f"https://api.stackexchange.com/2.3/{endpoint}/{ident}"
        j = (await self.client.get(api, params={"site": site})).json()
        items = j.get("items") or []
        if not items:
            return _result("stackexchange_api", url, ok=True, error="not found", api=api)
        it = items[0]
        return _result("stackexchange_api", url, created_ts=float(it.get("creation_date") or 0) or None,
                       edited_ts=float(it["last_edit_date"]) if it.get("last_edit_date") else None, api=api,
                       quality={"score": it.get("score"), "is_accepted": it.get("is_accepted"), "accepted_answer_id": it.get("accepted_answer_id"),
                                "answer_count": it.get("answer_count"), "site": site})

    async def _snowflake(self, url: str) -> dict[str, Any]:
        m = re.search(r"/status(?:es)?/(\d{15,})", url)
        if not m:
            return _result("snowflake", url, ok=True, error="no status id in URL")
        ts = snowflake_ts(int(m.group(1)))
        return _result("snowflake", url, created_ts=ts, quality={"note": "timestamp decoded from the post id; content itself not fetched (login-walled)"})

    async def _discourse(self, url: str) -> dict[str, Any]:
        base = re.sub(r"[?#].*$", "", url).rstrip("/")
        base = re.sub(r"(/t/[^/]+/\d+)(/\d+)?$", r"\1", base)
        api = base + ".json"
        r = await self.client.get(api)
        if r.status_code != 200:
            return _result("discourse", url, ok=True, error=f"discourse HTTP {r.status_code}", api=api)
        j = r.json()
        created = j.get("created_at")
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        return _result("discourse", url, created_ts=ts, api=api,
                       quality={"posts_count": j.get("posts_count"), "views": j.get("views"), "like_count": j.get("like_count")})

    async def _github_api(self, url: str) -> dict[str, Any]:
        m = re.search(r"github\.com/([^/]+)/([^/]+)/(issues|pull|discussions)/(\d+)", url)
        if not m:
            # repository page / README / raw file: dated by the repository's creation (its current text is not dated)
            m2 = re.search(r"(?:github\.com|raw\.githubusercontent\.com)/([^/?#]+)/([^/?#]+)", url)
            if not m2 or m2.group(1).lower() in ("orgs", "users", "search", "topics", "settings", "marketplace", "sponsors"):
                return _result("github_api", url, ok=True, error="not a repository, issue, PR or discussion URL")
            o, r_ = m2.groups()
            api = f"https://api.github.com/repos/{o}/{r_}"
            j = (await self.client.get(api, headers=self.structured.gh_headers)).json()
            created = j.get("created_at")
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
            return _result("github_api", url, created_ts=ts, api=api, note="repository creation date; page text itself is undated",
                           quality={"stars": j.get("stargazers_count"), "fork": j.get("fork")})
        o, r_, kind, n = m.groups()
        api = f"https://api.github.com/repos/{o}/{r_}/{'issues' if kind != 'pull' else 'pulls'}/{n}"
        j = (await self.client.get(api, headers=self.structured.gh_headers)).json()
        created = j.get("created_at")
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        return _result("github_api", url, created_ts=ts, api=api, quality={"state": j.get("state"), "comments": j.get("comments")})

    async def _huggingface_api(self, url: str) -> dict[str, Any]:
        m = re.search(r"(?:huggingface\.co|hf\.co)/(?:(datasets|spaces)/)?([^/?#]+)/([^/?#]+)", url)
        if not m or m.group(2).lower() in ("api", "docs", "blog", "papers", "collections", "settings"):
            return _result("huggingface_api", url, ok=True, error="not a model / dataset / space URL")
        kind, o, r_ = m.groups()
        api = f"https://huggingface.co/api/{kind or 'models'}/{o}/{r_}"
        j = (await self.client.get(api)).json()
        created = j.get("createdAt")
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() if created else None
        return _result("huggingface_api", url, created_ts=ts, api=api, note="repository creation date; page text itself is undated",
                       quality={"downloads": j.get("downloads"), "likes": j.get("likes")})

    async def _jsonld(self, url: str) -> dict[str, Any]:
        r = await self.client.get(url, headers={"Accept": "text/html"})
        html = r.text[:400_000]
        ts = None
        for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, flags=re.S | re.I):
            try:
                j = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            objs = j if isinstance(j, list) else [j]
            for o in objs:
                if isinstance(o, dict) and o.get("datePublished"):
                    ts = _parse_dt(o["datePublished"]); break
            if ts:
                break
        if not ts:
            m = re.search(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished|date|pubdate)["\'][^>]+content=["\']([^"\']+)', html, flags=re.I)
            if m:
                ts = _parse_dt(m.group(1))
        if not ts:
            m = re.search(r'<time[^>]+datetime=["\']([^"\']+)', html, flags=re.I)
            if m:
                ts = _parse_dt(m.group(1))
        return _result("jsonld", url, created_ts=ts, quality={"self_reported": True})

    async def _wayback(self, url: str) -> dict[str, Any]:
        wb = await self.structured.wayback_first_seen(url)
        if not wb.get("ok"):
            return _result("wayback", url, ok=False, error=wb.get("error"))
        if not wb.get("found"):
            return _result("wayback", url, ok=True, error="no Wayback capture of this URL")
        ts = datetime.fromisoformat(wb["first_snapshot"]).replace(tzinfo=timezone.utc).timestamp()
        return _result("wayback", url, created_ts=ts, api=wb.get("source"))


def _parse_dt(s: str) -> float | None:
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:20], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


async def domain_first_seen(etld1: str, ct_detail: dict | None, structured: Structured) -> dict[str, Any]:
    """Earliest independent evidence that a domain existed: min(CT first cert, Wayback first capture)."""
    cands: list[tuple[str, float]] = []
    if ct_detail and ct_detail.get("first_seen"):
        try:
            cands.append(("ct", datetime.fromisoformat(ct_detail["first_seen"]).timestamp()))
        except ValueError:
            pass
    wb = await structured.wayback_first_seen(etld1)
    if wb.get("ok") and wb.get("found"):
        cands.append(("wayback", datetime.fromisoformat(wb["first_snapshot"]).replace(tzinfo=timezone.utc).timestamp()))
    if not cands:
        return {"ok": False, "age_days": None}
    src, ts = min(cands, key=lambda x: x[1])
    return {"ok": True, "source": src, "first_seen_ts": ts, "age_days": _age(ts)}


async def age_many(aging: Aging, urls: list[str], limit: int = 6) -> dict[str, dict[str, Any]]:
    sem = asyncio.Semaphore(limit)
    async def one(u):
        async with sem:
            return u, await aging.age_of(u)
    pairs = await asyncio.gather(*(one(u) for u in urls))
    return dict(pairs)
