"""Source tiering. Tier 1 is a fixed whitelist that the LLM cannot extend."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ..cache.anchors import anchor_for
from ..checks.urltools import etld1_of, host_of

if TYPE_CHECKING:
    from ..config import IdentityConfig

TIER1_DOMAINS = {
    "wikidata.org", "wikipedia.org", "archive.org",
    # distribution package manifests / app stores (human-reviewed upstream URLs)
    "debian.org", "ubuntu.com", "archlinux.org", "fedoraproject.org", "opensuse.org", "gentoo.org", "alpinelinux.org",
    "brew.sh", "flathub.org", "snapcraft.io", "winget.run", "chocolatey.org", "scoop.sh", "nixos.org", "freebsd.org",
    # package registries (metadata, not arbitrary user content)
    "pypi.org", "pythonhosted.org", "npmjs.com", "npmjs.org", "crates.io", "rubygems.org", "packagist.org", "nuget.org", "maven.org", "pkg.go.dev", "hex.pm", "cran.r-project.org",
    # app stores
    "apple.com", "google.com", "microsoft.com",
}
TIER2_DOMAINS = {
    # reputable tech media (extend via config if needed)
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com", "zdnet.com", "venturebeat.com", "theregister.com",
    "bleepingcomputer.com", "thehackernews.com", "reuters.com", "bloomberg.com", "bbc.com", "bbc.co.uk", "nytimes.com",
    "ithome.com.tw", "technews.tw", "inside.com.tw", "ithome.com", "36kr.com", "infoq.com", "heise.de", "golem.de",
    "linkedin.com", "crunchbase.com", "ycombinator.com",
    # platform verification endpoints (org profile pages / API)
    "github.com", "huggingface.co", "gitlab.com",
    # cloud / hosting vendor technical documentation (editorially maintained how-to guides)
    "vultr.com", "digitalocean.com", "linode.com", "akamai.com", "hetzner.com", "ovhcloud.com", "cloudflare.com",
    "amazon.com", "oracle.com", "ibm.com", "redhat.com", "suse.com", "canonical.com", "nvidia.com", "intel.com", "amd.com",
    "thundercompute.com", "runpod.io", "lambda.ai", "paperspace.com",
}
TIER3_DOMAINS = {
    "reddit.com", "news.ycombinator.com", "stackoverflow.com", "stackexchange.com", "medium.com", "dev.to", "hashnode.dev",
    "quora.com", "discord.com", "discord.gg", "t.me", "telegram.org", "x.com", "twitter.com", "facebook.com", "youtube.com",
    "substack.com", "blogspot.com", "wordpress.com", "tumblr.com", "pinterest.com", "linktr.ee", "ptt.cc", "dcard.tw",
    "zhihu.com", "bilibili.com", "csdn.net", "juejin.cn",
}
# Official package-manager / distribution manifest repositories on generic code hosts are matched by "host/path"
# prefix; the list lives in data/tier1_paths.yaml (editable, re-read when its mtime changes; see tier1_path_prefixes()).
TIER1_PATHS_FILE = Path(__file__).resolve().parent.parent / "data" / "tier1_paths.yaml"
_T1_CACHE: dict = {"mtime": None, "set": set(), "error": None}


def tier1_path_prefixes() -> set[str]:
    """Prefixes from data/tier1_paths.yaml, normalised like config extras. A missing or broken file yields an empty
    set and records the error (tier1_paths_status); it never raises."""
    try:
        mtime = TIER1_PATHS_FILE.stat().st_mtime
    except OSError as e:
        _T1_CACHE.update(mtime=None, set=set(), error=f"{type(e).__name__}: {e}")
        return set()
    if _T1_CACHE["mtime"] != mtime:
        try:
            data = yaml.safe_load(TIER1_PATHS_FILE.read_text(encoding="utf-8")) or {}
            items = data.get("tier1_paths", []) if isinstance(data, dict) else data
            _, prefixes = _split_extra(items or [])
            _T1_CACHE.update(mtime=mtime, set=prefixes, error=None)
        except Exception as e:  # noqa: BLE001
            _T1_CACHE.update(mtime=mtime, set=set(), error=f"{type(e).__name__}: {e}")
    return _T1_CACHE["set"]


def tier1_paths_status() -> dict:
    prefixes = tier1_path_prefixes()
    return {"path": str(TIER1_PATHS_FILE), "prefixes": sorted(prefixes), "count": len(prefixes), "error": _T1_CACHE["error"]}


FORUM_HINTS = ("forum", "community", "discuss", "board", "bbs", "/t/", "/thread", "/topic", "reddit", "comments")


# Hosting platforms whose repository pages, READMEs, issues and discussions are user-generated content: anyone can
# publish there, so such pages are tier 3 (countable only with proven age). The platform's own records (API hosts,
# bare owner-profile roots) stay tier 2. All domains of one platform form ONE source family for independence counting.
PLATFORM_FAMILIES = {"github", "gitlab", "codeberg", "huggingface"}


def family_of(etld1: str) -> str:
    """Independence family of a source domain: Wikipedia+Wikidata are one, every domain of a hosting platform
    (github.com + githubusercontent.com + github.io, huggingface.co + hf.co, ...) is one, anything else is itself."""
    if etld1 in ("wikipedia.org", "wikidata.org"):
        return "wikimedia"
    a = anchor_for(etld1)
    return a.platform if a else etld1


def is_platform_endpoint(source_url: str) -> bool:
    """The platform's own verification data rather than user content: an API host, an /api/ path, or a bare
    owner-profile root such as github.com/<org> or huggingface.co/<org>."""
    hp = _host_path(source_url)
    host, _, path = hp.partition("/")
    if host.startswith("api."):
        return True
    path = path.strip("/")
    if path.startswith("api/"):
        return True
    return len([seg for seg in path.split("/") if seg]) <= 1


def classify(source_url: str, llm_proposed: int | None = None, extra: "IdentityConfig | None" = None,
             api_record: bool = False) -> tuple[int, str]:
    """Return (tier, reason). Built-in lists + config extras (identity.extra_tier1/2/3).
    The LLM may propose 2 or 3 for unknown domains; it can never produce tier 1.
    `api_record`: the evidence is a structured tool record (JSON from our own API call), not a fetched page; such
    records keep the platform's tier even when their URL is a repository page (e.g. a Hugging Face model page)."""
    host = host_of(source_url)
    e1 = etld1_of(host)
    x1, p1 = _split_extra(getattr(extra, "extra_tier1", []))
    x2, p2 = _split_extra(getattr(extra, "extra_tier2", []))
    x3, p3 = _split_extra(getattr(extra, "extra_tier3", []))
    t1 = TIER1_DOMAINS | x1
    t2 = TIER2_DOMAINS | x2
    t3 = TIER3_DOMAINS | x3
    hp = _host_path(source_url)
    # path prefixes first: they are more specific than the host (a manifest repo on github.com outranks "github.com")
    for tier, prefixes, why in ((1, tier1_path_prefixes() | p1, "official package manifest repository"),
                                (3, p3, "forum / social / blog path (config)"), (2, p2, "known tier-2 path (config)")):
        hit = next((pfx for pfx in prefixes if hp.startswith(pfx)), None)
        if hit:
            return tier, f"{hit} is a tier-{tier} {why}"
    fam = family_of(e1)
    if fam in PLATFORM_FAMILIES and not api_record and not is_platform_endpoint(source_url):
        return 3, f"user content on {fam} (repository page, README, issue or discussion): anyone can publish it; counts only with proven age"
    if e1 in t1 or host in t1:
        return 1, f"{e1} is a tier-1 structured source"
    if e1 in t3 or host in t3:
        return 3, f"{e1} is a forum / social / blog source"
    if e1 in t2 or host in t2:
        return 2, f"{e1} is a known tier-2 source"
    if any(h in source_url.lower() for h in FORUM_HINTS):
        return 3, f"{e1} looks like a forum / discussion page"
    if llm_proposed in (2, 3):
        return llm_proposed, f"unknown domain {e1}; LLM proposed tier {llm_proposed}"
    return 3, f"unknown domain {e1}; defaulted to tier 3"


def _host_path(url: str) -> str:
    """'https://www.GitHub.com/Org/Repo/x?y' -> 'github.com/org/repo/x' (lower-case, no scheme/www/query; always ends
    with '/' when there is no path so prefix matching cannot cross a path segment)."""
    u = (url or "").strip()
    rest = u.split("://", 1)[1] if "://" in u else u
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    host, _, path = rest.partition("/")
    host = host.lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}/{path.lower()}" if path else f"{host}/"


def _split_extra(items) -> tuple[set[str], set[str]]:
    """Config entries: 'host' or 'etld+1' go to the domain set; anything containing '/' is a host/path prefix."""
    domains, prefixes = set(), set()
    for raw in items or []:
        v = str(raw).lower().strip()
        if not v:
            continue
        v = v.split("://", 1)[1] if "://" in v else v
        v = v[4:] if v.startswith("www.") else v
        if "/" in v.rstrip("/"):
            prefixes.add(v.rstrip("/") + "/")
        else:
            domains.add(v.rstrip("/"))
    return domains, prefixes


def _lower(items) -> set[str]:
    return {str(x).lower().strip() for x in (items or []) if str(x).strip()}
