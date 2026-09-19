"""Built-in platform anchors: hosting platforms whose *root domain* is trusted, but whose
*path* (owner / org / package) still needs identity verification (L1).

`path_identity` describes how to extract the owner from the URL path.
`expected_issuers` are hints only (observed issuer drift is a warning, not a failure).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Anchor:
    platform: str
    etld1s: list[str]                          # root domains belonging to this platform (incl. CDNs)
    path_owner_re: str | None = None           # regex with named groups owner / repo
    expected_issuers: list[str] = field(default_factory=list)
    description: str = ""


SEED: list[Anchor] = [
    Anchor("github", ["github.com", "githubusercontent.com", "github.io"],
           r"^/(?P<owner>[A-Za-z0-9_.-]+)(?:/(?P<repo>[A-Za-z0-9_.-]+))?",
           ["DigiCert", "Sectigo"], "GitHub code hosting + release assets"),
    Anchor("gitlab", ["gitlab.com"], r"^/(?P<owner>[A-Za-z0-9_.-]+)(?:/(?P<repo>[A-Za-z0-9_.-]+))?",
           ["Cloudflare", "Let's Encrypt", "Sectigo"], "GitLab"),
    Anchor("codeberg", ["codeberg.org"], r"^/(?P<owner>[A-Za-z0-9_.-]+)(?:/(?P<repo>[A-Za-z0-9_.-]+))?",
           ["Let's Encrypt"], "Codeberg"),
    Anchor("huggingface", ["huggingface.co", "hf.co"],
           r"^/(?:(?:models|datasets|spaces)/)?(?P<owner>[A-Za-z0-9_.-]+)(?:/(?P<repo>[A-Za-z0-9_.-]+))?",
           ["Amazon", "Let's Encrypt", "Google Trust Services"], "Hugging Face Hub"),
    Anchor("pypi", ["pypi.org", "pythonhosted.org"],
           r"^/(?:project|simple)/(?P<owner>[A-Za-z0-9_.-]+)",
           ["GlobalSign", "Let's Encrypt", "DigiCert"], "PyPI (owner = package name)"),
    Anchor("npm", ["npmjs.com", "npmjs.org"],
           r"^/package/(?P<owner>@?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)",
           ["Google Trust Services", "DigiCert", "Amazon", "Cloudflare"], "npm registry (owner = package name)"),
    Anchor("crates", ["crates.io"], r"^/crates/(?P<owner>[A-Za-z0-9_-]+)",
           ["Amazon", "Let's Encrypt"], "crates.io (owner = crate name)"),
    Anchor("nuget", ["nuget.org"],
           r"(?i)^/(?:packages|api/v2/package|v3-flatcontainer)/(?P<owner>[A-Za-z0-9_.-]+)",
           ["Microsoft", "DigiCert"], "NuGet (owner = package ID; identity still requires L1)"),
    Anchor("dockerhub", ["docker.com", "docker.io"],
           r"^/(?:r|_)/(?P<owner>[A-Za-z0-9_.-]+)(?:/(?P<repo>[A-Za-z0-9_.-]+))?",
           ["DigiCert", "Amazon"], "Docker Hub"),
    Anchor("sourceforge", ["sourceforge.net", "sf.net"], r"^/projects/(?P<owner>[A-Za-z0-9_.-]+)",
           ["Sectigo", "Let's Encrypt"], "SourceForge (mirror network; owner = project slug)"),
    Anchor("flathub", ["flathub.org"], r"^/apps/(?P<owner>[A-Za-z0-9_.-]+)",
           ["Let's Encrypt"], "Flathub (owner = app id)"),
]

_BY_ETLD1: dict[str, Anchor] = {d: a for a in SEED for d in a.etld1s}


def anchor_for(etld1: str) -> Anchor | None:
    return _BY_ETLD1.get(etld1.lower())


def extract_path_identity(anchor: Anchor, path: str) -> tuple[str | None, str | None]:
    if not anchor.path_owner_re:
        return None, None
    m = re.match(anchor.path_owner_re, path or "/")
    if not m:
        return None, None
    gd = m.groupdict()
    return gd.get("owner"), gd.get("repo")


def all_anchor_domains() -> list[str]:
    return sorted(_BY_ETLD1.keys())
