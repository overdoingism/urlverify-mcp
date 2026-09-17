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
FORUM_HINTS = ("forum", "community", "discuss", "board", "bbs", "/t/", "/thread", "/topic", "reddit", "comments")


def classify(source_url: str, llm_proposed: int | None = None, extra: "IdentityConfig | None" = None) -> tuple[int, str]:
    """Return (tier, reason). Built-in lists + config extras (identity.extra_tier1/2/3).
    The LLM may propose 2 or 3 for unknown domains; it can never produce tier 1."""
    host = host_of(source_url)
    e1 = etld1_of(host)
    t1 = TIER1_DOMAINS | _lower(getattr(extra, "extra_tier1", []))
    t2 = TIER2_DOMAINS | _lower(getattr(extra, "extra_tier2", []))
    t3 = TIER3_DOMAINS | _lower(getattr(extra, "extra_tier3", []))
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


def _lower(items) -> set[str]:
    return {str(x).lower().strip() for x in (items or []) if str(x).strip()}
