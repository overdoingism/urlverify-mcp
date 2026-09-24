"""Distribution packages (apt / dnf / pacman): deterministic decision from the package manager's own origin report.

What a host installs depends on ITS repository configuration, which this server cannot see. The caller therefore
passes the package manager's report in `options.origin`:
    apt     `apt-cache policy <package>`   (candidate version and the archive URL(s) it comes from)
    dnf     `dnf info <package>`           (Repository / From repo)
    pacman  `pacman -Si <package>`         (Repository)
A package whose candidate comes only from the distribution's official archive is the distribution's official
channel (built and signed by the distribution): VERIFIED_TRUE with the notice DISTRO_PACKAGE. An apt package from a
third-party repository becomes a URL subject (the repository URL is verified like any download site). Anything else
is UNVERIFIABLE with a code that says what to do.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# Official archive hosts (suffix match). Other mirrors can be added in config: source.distro_archives.
APT_OFFICIAL_SUFFIXES = ("debian.org", "ubuntu.com")
DNF_OFFICIAL_REPOS = {"fedora", "updates", "baseos", "appstream", "extras", "crb", "powertools", "highavailability",
                      "resilientstorage", "rt", "nfv", "sap", "saphana"}
DNF_OFFICIAL_PREFIXES = ("rhel-", "fedora-")          # e.g. rhel-9-for-x86_64-baseos-rpms
PACMAN_OFFICIAL_REPOS = {"core", "extra", "multilib"}

ORIGIN_HOWTO = {"apt": "LC_ALL=C apt-cache policy {pkg}", "dnf": "LC_ALL=C dnf info {pkg}", "pacman": "LC_ALL=C pacman -Si {pkg}"}


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def name_matches(project: str, package: str) -> bool:
    p, n = _norm(project), _norm(package)
    return len(p) >= 3 and len(n) >= 3 and (p in n or n in p)


def parse_apt_policy(text: str, pkg: str) -> dict[str, Any] | None:
    """{candidate, installed, versions: {version: [origin URLs]}} for `pkg`, or None if the report has no block for it."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(rf"{re.escape(pkg)}(:\S+)?:\s*", ln.strip())), None)
    if start is None:
        return None
    out: dict[str, Any] = {"candidate": None, "installed": None, "versions": {}}
    cur = None
    header = 0                                               # apt always prints Installed, then Candidate, then the table
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():
            break                                            # next package block
        st = ln.strip()
        kv = re.match(r"^[^:：]+[:：]\s*(\S*)\s*$", st)      # any locale: "Candidate:", "候選：" ...
        if header < 2 and kv and not re.match(r"^-?\d+\s", st):
            out["installed" if header == 0 else "candidate"] = kv.group(1) or None
            header += 1
        elif header >= 2 and kv and not out["versions"] and not re.match(r"^(\*\*\*\s+)?\S+\s+-?\d+$", st):
            header += 1                                      # "Version table:" line
        elif re.match(r"^(\*\*\*\s+)?\S+\s+-?\d+$", st) and not st.startswith(("http", "/")):
            cur = st.replace("***", "").split()[0]
            out["versions"].setdefault(cur, [])
        elif cur and re.match(r"^-?\d+\s+(https?://\S+)", st):
            m = re.match(r"^-?\d+\s+(https?://\S+)\s+(\S+)", st)
            out["versions"][cur].append({"url": m.group(1), "suite": m.group(2)})
    return out


def parse_field(text: str, pkg: str, fields: tuple[str, ...]) -> str | None:
    """`Name : pkg` block of dnf info / pacman -Si; returns the first of `fields` in that block."""
    blocks = re.split(r"\n\s*\n", text)
    for b in blocks:
        if re.search(rf"^\s*Name\s*:\s*{re.escape(pkg)}\s*$", b, re.M | re.I):
            for f in fields:
                m = re.search(rf"^\s*{f}\s*:\s*(\S+)", b, re.M | re.I)
                if m:
                    return m.group(1)
    return None


def _apt_official(url: str, extra: list[str]) -> bool:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    hp = f"{host}{u.path}".rstrip("/").lower()
    if any(host == s or host.endswith("." + s) for s in APT_OFFICIAL_SUFFIXES):
        return True
    return any(hp == e.lower().rstrip("/") or hp.startswith(e.lower().rstrip("/") + "/") or host == e.lower() for e in extra)


def decide(subject, project: str, origin: str | None, extra_archives: list[str]) -> dict[str, Any]:
    """Returns {"verdict", "confidence", "codes", "notices"} or {"url": repository URL} (verify it like a download site)."""
    mgr, pkg = subject.options.get("manager"), subject.name or ""
    howto = ORIGIN_HOWTO[mgr].format(pkg=pkg)
    if not origin or not origin.strip():
        return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_NEEDED"],
                "notices": [], "message": f"run `{howto}` on the target machine and pass its output as options.origin"}
    notices = ["DISTRO_PACKAGE"]
    if mgr == "apt":
        rep = parse_apt_policy(origin, pkg)
        if rep is None:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_MISSING_PACKAGE"], "notices": [],
                    "message": f"options.origin has no block for {pkg}; pass the output of `{howto}`"}
        if not rep["candidate"] or rep["candidate"] == "(none)":
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_PACKAGE_NOT_AVAILABLE"], "notices": []}
        ver = subject.version_spec or rep["candidate"]
        origins = rep["versions"].get(ver)
        if origins is None:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["VERSION_NOT_FOUND"], "notices": []}
        subject.version = ver
        urls = [o["url"] for o in origins]
        if not urls:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_UNKNOWN"], "notices": []}
        official = [_apt_official(u, extra_archives) for u in urls]
        subject.registry = urls[0]
        subject.registry_basis = "options.origin (apt-cache policy)"
        if all(official):
            pass
        elif not any(official):
            third = sorted({f"{urlsplit(u).scheme}://{urlsplit(u).netloc}{urlsplit(u).path}".rstrip("/") for u in urls})
            if len(third) == 1:
                return {"url": third[0]}
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_MIXED"], "notices": []}
        else:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_MIXED"], "notices": []}
    else:
        repo = parse_field(origin, pkg, ("Repository", "From repo"))
        if not repo:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["DISTRO_ORIGIN_MISSING_PACKAGE"], "notices": [],
                    "message": f"options.origin has no Repository line for {pkg}; pass the output of `{howto}`"}
        repo_l = repo.lower().lstrip("@")
        subject.registry, subject.registry_basis = repo, f"options.origin ({howto.split()[1]})"
        ok = (repo_l in PACMAN_OFFICIAL_REPOS) if mgr == "pacman" else (repo_l in DNF_OFFICIAL_REPOS or repo_l.startswith(DNF_OFFICIAL_PREFIXES))
        if not ok:
            return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": [f"DISTRO_REPOSITORY_NOT_OFFICIAL:{repo}"], "notices": []}
        v = parse_field(origin, pkg, ("Version",))
        subject.version = v
        if subject.version_spec and v and subject.version_spec != v:
            notices.append("VERSION_NOT_ENFORCED")
    if not name_matches(project, pkg):
        return {"verdict": "UNVERIFIABLE", "confidence": 0.0, "codes": ["PROJECT_NAME_NOT_MATCHED"], "notices": notices}
    return {"verdict": "VERIFIED_TRUE", "confidence": 0.9, "codes": [], "notices": notices}
