"""Source tiering. Tier 1 is a fixed whitelist that the LLM cannot extend."""
from __future__ import annotations

from typing import TYPE_CHECKING

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
# Official package-manager / distribution manifest repositories that live on a generic code host. The host alone
# (github.com, raw.githubusercontent.com) says nothing, so these are matched as "host/path" prefixes, case-insensitive.
# One or two lines per package manager; packages themselves are never listed.
TIER1_PATH_PREFIXES = {
    "github.com/microsoft/winget-pkgs/", "raw.githubusercontent.com/microsoft/winget-pkgs/",
    "github.com/homebrew/homebrew-core/", "github.com/homebrew/homebrew-cask/",
    "raw.githubusercontent.com/homebrew/homebrew-core/", "raw.githubusercontent.com/homebrew/homebrew-cask/",
    "github.com/scoopinstaller/", "raw.githubusercontent.com/scoopinstaller/",
    "github.com/chocolatey-community/chocolatey-packages/", "raw.githubusercontent.com/chocolatey-community/chocolatey-packages/",
    "github.com/nixos/nixpkgs/", "raw.githubusercontent.com/nixos/nixpkgs/",
    "github.com/flathub/", "raw.githubusercontent.com/flathub/",
    "github.com/macports/macports-ports/", "raw.githubusercontent.com/macports/macports-ports/",
    "github.com/conda-forge/", "raw.githubusercontent.com/conda-forge/",
    "github.com/f-droid/fdroiddata/", "gitlab.com/fdroid/fdroiddata/",
    "github.com/void-linux/void-packages/", "raw.githubusercontent.com/void-linux/void-packages/",
    "github.com/freebsd/freebsd-ports/", "raw.githubusercontent.com/freebsd/freebsd-ports/",
    "github.com/gentoo/gentoo/", "raw.githubusercontent.com/gentoo/gentoo/",
    "github.com/alpinelinux/aports/", "raw.githubusercontent.com/alpinelinux/aports/",
    "github.com/msys2/mingw-packages/", "github.com/msys2/msys2-packages/",
    "raw.githubusercontent.com/msys2/mingw-packages/", "raw.githubusercontent.com/msys2/msys2-packages/",
}
FORUM_HINTS = ("forum", "community", "discuss", "board", "bbs", "/t/", "/thread", "/topic", "reddit", "comments")


def classify(source_url: str, llm_proposed: int | None = None, extra: "IdentityConfig | None" = None) -> tuple[int, str]:
    """Return (tier, reason). Built-in lists + config extras (identity.extra_tier1/2/3).
    The LLM may propose 2 or 3 for unknown domains; it can never produce tier 1."""
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
    for tier, prefixes, why in ((1, TIER1_PATH_PREFIXES | p1, "official package manifest repository"),
                                (3, p3, "forum / social / blog path (config)"), (2, p2, "known tier-2 path (config)")):
        hit = next((pfx for pfx in prefixes if hp.startswith(pfx)), None)
        if hit:
            return tier, f"{hit} is a tier-{tier} {why}"
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
