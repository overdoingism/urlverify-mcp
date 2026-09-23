"""Resolve parsed subjects to the exact thing that would be installed: version from the registry, WinGet installer URL
from the official manifest. Only registry / manifest JSON and YAML are fetched; packages are never downloaded.

A registry that does not know the package is not an error here: the subject still gets its registry URL and the
pipeline's registry-state rule decides (a missing package is VERIFIED_FALSE, as before)."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from ..health import observe
from .model import Seed, Subject
from .semver import max_satisfying, parse_version

WINGET_REPO = "microsoft/winget-pkgs"
_ARCH = {"x64": "x64", "amd64": "x64", "x86_64": "x64", "x86": "x86", "i386": "x86", "32-bit": "x86",
         "arm64": "arm64", "aarch64": "arm64", "arm": "arm", "neutral": "neutral"}
MAX_WINGET_INSTALLERS = 4


class Resolver:
    def __init__(self, timeout: float, user_agent: str, github_token: str = "", transport: httpx.AsyncBaseTransport | None = None):
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True, transport=transport)
        self.gh_headers = {"Accept": "application/vnd.github+json"}
        if github_token:
            self.gh_headers["Authorization"] = f"Bearer {github_token}"

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, url: str, dep: str, headers: dict | None = None) -> httpx.Response:
        try:
            r = await self.client.get(url, headers=headers)
        except httpx.HTTPError as e:
            observe(dep, False, f"{type(e).__name__}: {e}")
            raise
        observe(dep, r.status_code < 500, None if r.status_code < 500 else f"HTTP {r.status_code}")
        return r

    async def resolve(self, s: Subject, hint_text: str = "") -> list[Subject]:
        if s.blocked:
            return [s]
        try:
            if s.ecosystem == "pypi":
                await self._pypi(s)
            elif s.ecosystem == "npm":
                await self._npm(s)
            elif s.ecosystem == "nuget":
                await self._nuget(s)
            elif s.ecosystem == "winget":
                return await self._winget(s, hint_text)
        except httpx.HTTPError as e:
            s.codes.append(f"RESOLUTION_FAILED:{s.ecosystem}")
            s.notes.append(f"{type(e).__name__}: {e}"[:200])
        except ValueError as e:
            s.codes.append("VERSION_RANGE_UNSUPPORTED")
            s.notes.append(str(e)[:200])
        return [s]

    # ------------------------------------------------------------------------------------------------ PyPI
    async def _pypi(self, s: Subject) -> None:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
        name = s.name or ""
        r = await self._get(f"https://pypi.org/pypi/{quote(name)}/json", "pypi")
        s.url = f"https://pypi.org/project/{name}/"
        if r.status_code == 404:
            s.notes.append("PACKAGE_NOT_FOUND")
            return
        r.raise_for_status()
        doc = r.json()
        releases: dict[str, list] = doc.get("releases") or {}
        usable = [v for v, files in releases.items() if files and not all(f.get("yanked") for f in files)]
        if not s.version_spec:
            s.version = (doc.get("info") or {}).get("version")
        else:
            try:
                spec = SpecifierSet(s.version_spec)
            except InvalidSpecifier:
                raise ValueError(f"not a PEP 440 specifier: {s.version_spec}")
            exact = [x for x in spec if x.operator in ("==", "===") and "*" not in x.version]
            if exact and len(list(spec)) == 1:
                wanted = exact[0].version
                hit = next((v for v in releases if v == wanted or _same_pep440(v, wanted)), None)
                s.version = hit
            else:
                cands = []
                for v in usable:
                    try:
                        cands.append(Version(v))
                    except InvalidVersion:
                        pass
                ok = list(spec.filter(cands, prereleases=True if s.prerelease_ok else None))
                s.version = str(max(ok)) if ok else None
                if s.version:
                    s.version = next((v for v in usable if _same_pep440(v, s.version)), s.version)
            if not s.version:
                s.codes.append("VERSION_NOT_FOUND")
                return
        if s.version:
            s.url = f"https://pypi.org/project/{name}/{s.version}/"

    # ------------------------------------------------------------------------------------------------- npm
    async def _npm(self, s: Subject) -> None:
        name = s.name or ""
        enc = name.replace("/", "%2F")
        r = await self._get(f"https://registry.npmjs.org/{enc}", "npm")
        s.url = f"https://www.npmjs.com/package/{name}"
        if r.status_code == 404:
            s.notes.append("PACKAGE_NOT_FOUND")
            return
        r.raise_for_status()
        doc = r.json()
        tags: dict = doc.get("dist-tags") or {}
        versions = list((doc.get("versions") or {}).keys())
        spec = (s.version_spec or "").strip()
        if not spec:
            s.version = tags.get("latest")
        elif spec in tags:
            s.version = tags[spec]
        elif parse_version(spec) is not None:
            s.version = spec.lstrip("v") if spec.lstrip("v") in versions else None
        else:
            s.version = max_satisfying(versions, spec, include_prerelease=s.prerelease_ok)
        if not s.version:
            s.codes.append("VERSION_NOT_FOUND" if spec else "PACKAGE_HAS_NO_RELEASE")
            return
        s.url = f"https://www.npmjs.com/package/{name}/v/{s.version}"

    # ----------------------------------------------------------------------------------------------- NuGet
    async def _nuget(self, s: Subject) -> None:
        name = s.name or ""
        r = await self._get(f"https://api.nuget.org/v3-flatcontainer/{name.lower()}/index.json", "nuget")
        s.url = f"https://www.nuget.org/packages/{name}"
        if r.status_code == 404:
            s.notes.append("PACKAGE_NOT_FOUND")
            return
        r.raise_for_status()
        versions: list[str] = r.json().get("versions") or []
        spec = (s.version_spec or "").strip()
        if not spec:
            pool = versions if s.prerelease_ok else [v for v in versions if "-" not in v]
            s.version = pool[-1] if pool else None
        elif re.fullmatch(r"[\[(].*[\])]", spec) or "*" in spec:
            s.version = _nuget_range(versions, spec, s.prerelease_ok)
        else:
            s.version = next((v for v in versions if v.lower() == _nuget_norm(spec).lower()), None)
        if not s.version:
            s.codes.append("VERSION_NOT_FOUND" if spec else "PACKAGE_HAS_NO_RELEASE")
            return
        s.url = f"https://www.nuget.org/packages/{name}/{s.version}"

    # ---------------------------------------------------------------------------------------------- WinGet
    async def _winget(self, s: Subject, hint_text: str) -> list[Subject]:
        ident = s.name or ""
        path = f"manifests/{ident[0].lower()}/{'/'.join(ident.split('.'))}"
        r = await self._get(f"https://api.github.com/repos/{WINGET_REPO}/contents/{quote(path)}?ref=master", "github", self.gh_headers)
        if r.status_code == 404:
            s.codes.append("WINGET_QUERY_NOT_EXACT" if "WINGET_QUERY_TREATED_AS_ID" in s.notes else "WINGET_MANIFEST_NOT_FOUND")
            return [s]
        if r.status_code in (403, 429):
            s.codes.append("RESOLUTION_FAILED:winget")
            s.notes.append(f"GitHub API HTTP {r.status_code} (rate limit; set identity.github_token)")
            return [s]
        r.raise_for_status()
        dirs = [e["name"] for e in r.json() if e.get("type") == "dir" and re.match(r"^\d", e.get("name", ""))]
        if s.version_spec:
            ver = next((d for d in dirs if d == s.version_spec), None)
            if not ver:
                s.codes.append("VERSION_NOT_FOUND")
                return [s]
        else:
            ver = max(dirs, key=_loose_version_key) if dirs else None
            if not ver:
                s.codes.append("WINGET_MANIFEST_NOT_FOUND")
                return [s]
        base = f"https://raw.githubusercontent.com/{WINGET_REPO}/master/{path}/{ver}"
        text, murl = None, None
        for fname in (f"{ident}.installer.yaml", f"{ident}.yaml"):
            rr = await self._get(f"{base}/{quote(fname)}", "github")
            if rr.status_code == 200:
                text, murl = rr.text, f"{base}/{quote(fname)}"
                break
        if text is None:
            s.codes.append("WINGET_MANIFEST_NOT_FOUND")
            return [s]
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            doc = None
        if not isinstance(doc, dict) or str(doc.get("PackageIdentifier", "")).lower() != ident.lower():
            # e.g. a rate-limit page served in place of the file: a lookup failure, not a problem with the caller's input
            s.codes.append("RESOLUTION_FAILED:winget")
            s.notes.append("the manifest request did not return this package's manifest (GitHub rate limit?); retry later")
            return [s]
        installers = _winget_installers(doc)
        want_arch = _ARCH.get((s.options.get("architecture") or "").lower()) or _arch_from_text(hint_text)
        sel = [i for i in installers
               if (not want_arch or i.get("Architecture", "").lower() in (want_arch, "neutral"))
               and (not s.options.get("scope") or (i.get("Scope") or "").lower() in ("", s.options["scope"].lower()))
               and (not s.options.get("installer_type") or (i.get("InstallerType") or "").lower() == s.options["installer_type"].lower())
               and (not s.options.get("locale") or (i.get("InstallerLocale") or "").lower() in ("", s.options["locale"].lower()))]
        urls = list(dict.fromkeys(i["InstallerUrl"] for i in sel if i.get("InstallerUrl")))
        if not urls:
            s.version = ver
            s.codes.append("WINGET_INSTALLER_NOT_FOUND")
            return [s]
        if len(urls) > MAX_WINGET_INSTALLERS:
            s.version = ver
            s.codes.append("WINGET_TOO_MANY_INSTALLERS")
            s.notes.append("narrow it with --architecture / --scope / --installer-type, or name the architecture in `artifact`")
            return [s]
        out = []
        for u in urls:
            line = next((ln.strip() for ln in text.splitlines() if u in ln and "InstallerUrl" in ln), f"InstallerUrl: {u}")
            seed = Seed(kind="distro", source=murl, text=text,
                        claim=f"Microsoft's winget manifest for {ident} {ver} names this installer URL", quote=line)
            sub = s.model_copy(deep=True)
            sub.version, sub.url = ver, u
            sub.registry, sub.registry_basis = f"https://github.com/{WINGET_REPO}", "public default"
            sub.seeds.append(seed)
            if len(urls) > 1:
                sub.notes.append(f"WINGET_ONE_OF_{len(urls)}_INSTALLERS")
            out.append(sub)
        return out


def _same_pep440(a: str, b: str) -> bool:
    from packaging.version import InvalidVersion, Version
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _loose_version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-+]", v))


def _nuget_norm(v: str) -> str:
    parts = v.split("-", 1)
    nums = parts[0].split(".")
    while len(nums) > 3 and nums[-1] == "0":
        nums.pop()
    return ".".join(nums) + ("-" + parts[1] if len(parts) > 1 else "")


def _nuget_range(versions: list[str], spec: str, pre: bool) -> str | None:
    from packaging.version import InvalidVersion, Version

    def V(x):
        try:
            return Version(x)
        except InvalidVersion:
            return None
    cands = [v for v in versions if V(v) is not None and (pre or "-" not in v)]
    if "*" in spec:
        prefix = spec.split("*", 1)[0]
        hits = [v for v in cands if v.startswith(prefix)]
        return max(hits, key=V) if hits else None
    m = re.fullmatch(r"([\[(])\s*([^,\s]*)\s*(?:,\s*([^\])\s]*)\s*)?([\])])", spec)
    if not m:
        raise ValueError(f"not a NuGet version range: {spec}")
    lo_inc, lo, hi, hi_inc = m[1] == "[", m[2], m[3], m[4] == "]"
    if m[3] is None:                                   # [1.0] exact
        return next((v for v in cands if V(v) == V(lo)), None)
    def ok(v):
        x = V(v)
        if lo and (x < V(lo) or (x == V(lo) and not lo_inc)):
            return False
        if hi and (x > V(hi) or (x == V(hi) and not hi_inc)):
            return False
        return True
    hits = [v for v in cands if ok(v)]
    return max(hits, key=V) if hits else None


def _winget_installers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = {k: v for k, v in doc.items() if k != "Installers" and not isinstance(v, (list, dict))}
    out = []
    for i in doc.get("Installers") or []:
        if isinstance(i, dict):
            out.append({**defaults, **i})
    return out


def _arch_from_text(text: str) -> str | None:
    found = {_ARCH[m.lower()] for m in re.findall(r"(?i)(?<![\w-])(x64|amd64|x86_64|arm64|aarch64|x86|i386|32-bit)(?![\w-])", text or "")}
    return found.pop() if len(found) == 1 else None
