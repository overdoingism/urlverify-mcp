"""Built-in platform anchors: hosting platforms whose *root domain* is trusted, but whose *path* (owner / org /
package) still needs identity verification (L1).

Each platform declares exactly which hosts carry USER CONTENT and where the owner is read from (a path segment, a
host label, or nowhere for opaque asset/CDN hosts). Any other host on the platform's domains is the platform
company's own site (desktop.docker.com, docs.github.com): a normal website whose domain IS an identity.
`expected_issuers` are hints only (observed issuer drift is a warning, not a failure).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SEG = r"[A-Za-z0-9_.-]+"
_OWNER_REPO = rf"^/(?P<owner>{_SEG})(?:/(?P<repo>{_SEG}))?"


@dataclass
class HostRule:
    host_re: str                      # matched against the full lower-case host
    owner_in: str = "path"            # "path" (path_re has group owner), "host" (host_re has group owner), "opaque"
    path_re: str | None = None        # for owner_in == "path"


@dataclass
class Anchor:
    platform: str
    etld1s: list[str]                          # root domains belonging to this platform (incl. CDNs)
    hosts: list[HostRule] = field(default_factory=list)   # user-content hosts; anything else is the company's own site
    expected_issuers: list[str] = field(default_factory=list)
    description: str = ""



SEED: list[Anchor] = [
    Anchor("github", ["github.com", "githubusercontent.com", "github.io"], [
        HostRule(r"^github\.com$", "path", _OWNER_REPO),
        HostRule(r"^(?:raw|gist|user-images|avatars\d*|camo)\.githubusercontent\.com$", "path", _OWNER_REPO),
        HostRule(r"^codeload\.github\.com$", "path", _OWNER_REPO),
        HostRule(r"^(?P<owner>[a-z0-9-]+)\.github\.io$", "host"),
        HostRule(r"^[^.]+\.githubusercontent\.com$", "opaque"),          # release-assets, objects, ... (owner not in URL)
    ], ["DigiCert", "Sectigo"], "GitHub code hosting + release assets + Pages"),
    Anchor("gitlab", ["gitlab.com", "gitlab.io"], [
        HostRule(r"^gitlab\.com$", "path", _OWNER_REPO),
        HostRule(r"^(?P<owner>[a-z0-9-]+)\.gitlab\.io$", "host"),
    ], ["Cloudflare", "Let's Encrypt", "Sectigo"], "GitLab"),
    Anchor("codeberg", ["codeberg.org", "codeberg.page"], [
        HostRule(r"^codeberg\.org$", "path", _OWNER_REPO),
        HostRule(r"^(?P<owner>[a-z0-9-]+)\.codeberg\.page$", "host"),
    ], ["Let's Encrypt"], "Codeberg"),
    Anchor("huggingface", ["huggingface.co", "hf.co"], [
        HostRule(r"^(?:huggingface\.co|hf\.co)$", "path", rf"^/(?:(?:models|datasets|spaces)/)?(?P<owner>{_SEG})(?:/(?P<repo>{_SEG}))?"),
        HostRule(r"^cdn-lfs[^.]*\.(?:huggingface\.co|hf\.co)$", "opaque"),
    ], ["Amazon", "Let's Encrypt", "Google Trust Services"], "Hugging Face Hub"),
    Anchor("pypi", ["pypi.org", "pythonhosted.org"], [
        HostRule(r"^(?:www\.|test\.)?pypi\.org$", "path", rf"^/(?:project|simple)/(?P<owner>{_SEG})"),
        HostRule(r"^files\.pythonhosted\.org$", "opaque"),
    ], ["GlobalSign", "Let's Encrypt", "DigiCert"], "PyPI (owner = package name)"),
    Anchor("npm", ["npmjs.com", "npmjs.org"], [
        HostRule(r"^(?:www\.)?npmjs\.com$", "path", rf"^/package/(?P<owner>@{_SEG}/{_SEG}|{_SEG})"),
        HostRule(r"^registry\.npmjs\.org$", "path", rf"^/(?P<owner>@{_SEG}/{_SEG}|{_SEG})"),
    ], ["Google Trust Services", "DigiCert", "Amazon", "Cloudflare"], "npm registry (owner = package name)"),
    Anchor("crates", ["crates.io"], [
        HostRule(r"^crates\.io$", "path", r"^/crates/(?P<owner>[A-Za-z0-9_-]+)"),
        HostRule(r"^static\.crates\.io$", "opaque"),
    ], ["Amazon", "Let's Encrypt"], "crates.io (owner = crate name)"),
    Anchor("nuget", ["nuget.org"], [
        HostRule(r"^(?:www\.|api\.)?nuget\.org$", "path", rf"(?i)^/(?:packages|api/v2/package|v3-flatcontainer)/(?P<owner>{_SEG})"),
    ], ["Microsoft", "DigiCert"], "NuGet (owner = package ID; identity still requires L1)"),
    Anchor("dockerhub", ["docker.com", "docker.io"], [
        HostRule(r"^hub\.docker\.com$", "path", rf"^/(?:r|_|u)/(?P<owner>{_SEG})(?:/(?P<repo>{_SEG}))?"),
        HostRule(r"^(?:registry-1\.|index\.)?docker\.io$", "opaque"),
    ], ["DigiCert", "Amazon"], "Docker Hub (hub.docker.com); other docker.com hosts are Docker, Inc.'s own sites"),
    Anchor("sourceforge", ["sourceforge.net", "sf.net"], [
        HostRule(r"^(?:www\.)?(?:sourceforge\.net|sf\.net)$", "path", rf"^/projects/(?P<owner>{_SEG})"),
        HostRule(r"^(?:[a-z0-9-]+\.)?(?:dl|downloads)\.sourceforge\.net$", "path", rf"^/project/(?P<owner>{_SEG})"),
    ], ["Sectigo", "Let's Encrypt"], "SourceForge (mirror network; owner = project slug)"),
    Anchor("flathub", ["flathub.org"], [
        HostRule(r"^flathub\.org$", "path", rf"^/apps/(?P<owner>{_SEG})"),
        HostRule(r"^dl\.flathub\.org$", "opaque"),
    ], ["Let's Encrypt"], "Flathub (owner = app id)"),
]

_BY_ETLD1: dict[str, Anchor] = {d: a for a in SEED for d in a.etld1s}


def anchor_for(etld1: str) -> Anchor | None:
    return _BY_ETLD1.get(etld1.lower())


def resolve(anchor: Anchor, host: str, path: str) -> tuple[str, str | None, str | None]:
    """(scope, owner, repo) for a URL on this platform. scope is 'user_content' (owner from path / host, or None when
    the host is an opaque asset host) or 'company_site' (the platform operator's own website)."""
    host = (host or "").lower()
    path = path or "/"
    for rule in anchor.hosts:
        m = re.match(rule.host_re, host)
        if not m:
            continue
        if rule.owner_in == "opaque":
            return "user_content", None, None
        if rule.owner_in == "host":
            segs = [s for s in path.split("/") if s]
            return "user_content", m.group("owner"), (segs[0] if segs else None)
        pm = re.match(rule.path_re or "$^", path)
        if not pm:
            return "company_site", None, None            # front page / non-owner path of the user-content host
        return "user_content", pm.group("owner"), pm.groupdict().get("repo")
    return "company_site", None, None


def all_anchor_domains() -> list[str]:
    return sorted(_BY_ETLD1.keys())
