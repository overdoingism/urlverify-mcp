"""Registry release dates, independent of identity and malware scanning.

Only registry JSON is fetched; distribution URLs are compared, never downloaded.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urlsplit

import httpx

from ..config import Config
from ..health import observe
from ..models import CheckResult, VerifyRequest, VerifyResult


@dataclass
class ReleaseTarget:
    registry: str
    name: str = ""
    version: str | None = None
    artifact: str | None = None


def target_from_url(url: str) -> ReleaseTarget | None:
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    path = unquote(u.path).rstrip("/")
    if host in ("pypi.org", "www.pypi.org"):
        m = re.fullmatch(r"/(?:project|simple)/([\w.-]+)(?:/([^/]+))?", path)
        if not m:
            m = re.fullmatch(r"/pypi/([\w.-]+)(?:/([^/]+))?/json", path)
        return ReleaseTarget("pypi", m[1], m[2]) if m else ReleaseTarget("pypi")
    if host == "files.pythonhosted.org":
        filename = path.rsplit("/", 1)[-1]
        if filename.endswith(".whl"):
            parts = filename.split("-")
            if len(parts) in (5, 6):
                return ReleaseTarget("pypi", parts[0], parts[1], url)
        m = re.fullmatch(r"(.+)-([0-9][^-]*)\.(?:tar\.gz|zip)", filename)
        return ReleaseTarget("pypi", m[1], m[2], url) if m else ReleaseTarget("pypi", artifact=url)
    if host in ("npmjs.com", "www.npmjs.com", "registry.npmjs.org"):
        p = path.removeprefix("/package") if host != "registry.npmjs.org" else path
        m = re.fullmatch(r"/(@[\w.-]+/[\w.-]+|[\w.-]+)(.*)", p)
        if not m:
            return ReleaseTarget("npm")
        name, rest = m.groups()
        if rest.startswith("/-/") and rest.endswith(".tgz"):
            return ReleaseTarget("npm", name, artifact=url)
        if rest.startswith("/v/"):
            rest = rest[2:]
        if rest and not re.fullmatch(r"/[^/]+", rest):
            return ReleaseTarget("npm")
        return ReleaseTarget("npm", name, rest[1:] or None)
    if host in ("nuget.org", "www.nuget.org", "api.nuget.org", "globalcdn.nuget.org"):
        m = re.fullmatch(r"/(?:packages|api/v2/package)/([\w.-]+)(?:/([^/]+))?", path, re.I)
        if m:
            return ReleaseTarget("nuget", m[1], m[2])
        m = re.fullmatch(r"/v3-flatcontainer/([\w.-]+)/([^/]+)/([^/]+\.nupkg)", path, re.I)
        if m and m[3].lower() == f"{m[1]}.{m[2]}.nupkg".lower():
            return ReleaseTarget("nuget", m[1], m[2])
        # CDN filenames alone cannot unambiguously separate an ID from a version.
        return ReleaseTarget("nuget", artifact=url)
    return None


def _nuget_version(version: str) -> str:
    v = version.split("+", 1)[0].lower()
    core, sep, suffix = v.partition("-")
    parts = core.split(".")
    if not 1 <= len(parts) <= 4 or not all(p.isdigit() for p in parts):
        raise ValueError("invalid NuGet version")
    parts = [str(int(p)) for p in parts]
    parts += ["0"] * max(0, 3 - len(parts))
    if len(parts) == 4 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts) + (sep + suffix if sep else "")


class ReleaseMetadata:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def json(self, url: str) -> dict:
        # API-discovered links must stay within the registry's HTTPS API hosts.
        u = urlsplit(url)
        if u.scheme != "https" or u.hostname not in ("pypi.org", "registry.npmjs.org", "api.nuget.org") or u.port not in (None, 443) or u.username or u.password:
            raise ValueError("invalid registry metadata endpoint")
        r = await self.client.get(url, follow_redirects=False)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("invalid registry metadata")
        return data

    async def resolve(self, target: ReleaseTarget) -> dict:
        if not target.name:
            raise ValueError("URL does not identify a package version; use a registry package/version URL")
        return await getattr(self, target.registry)(target)

    async def npm(self, t: ReleaseTarget) -> dict:
        source = f"https://registry.npmjs.org/{quote(t.name, safe='@')}"
        doc = await self.json(source)
        requested = t.version or "latest"
        version = (doc.get("dist-tags") or {}).get(requested, requested)
        versions = doc.get("versions") or {}
        if t.artifact:
            matches = [v for v, record in versions.items() if (record.get("dist") or {}).get("tarball") == t.artifact]
            if len(matches) != 1:
                raise ValueError("artifact URL does not uniquely match registry metadata")
            version = matches[0]
        if version not in versions:
            raise ValueError("requested npm version is not available")
        return {"registry": "npm", "package": t.name, "version": version, "source": source,
                "published_at": (doc.get("time") or {}).get(version),
                "resolution": "artifact" if t.artifact else ("requested" if t.version else "latest"),
                "deprecated": versions[version].get("deprecated"), "artifact": t.artifact}

    async def pypi(self, t: ReleaseTarget) -> dict:
        source = f"https://pypi.org/pypi/{quote(t.name, safe='')}/"
        source += f"{quote(t.version, safe='')}/json" if t.version else "json"
        doc = await self.json(source)
        info = doc.get("info") or {}
        files = doc.get("urls") or []
        if t.artifact:
            files = [f for f in files if f.get("url") == t.artifact]
        if not files or not info.get("version"):
            raise ValueError("requested release/artifact is not available")
        stamps = [f.get("upload_time_iso_8601") for f in files]
        if not all(stamps):
            raise ValueError("release has files without upload timestamps")
        # Without a selected file, the newest upload is conservative: wheels can be added later.
        dates = [_date(s) for s in stamps]
        return {"registry": "pypi", "package": info.get("name") or t.name, "version": info["version"],
                "source": source, "published_at": max(dates).isoformat(), "artifact": t.artifact,
                "resolution": "artifact" if t.artifact else ("requested" if t.version else "latest"),
                "date_basis": "artifact_upload" if t.artifact else "newest_file_upload_in_version",
                "yanked": all(f.get("yanked", False) for f in files)}

    async def nuget(self, t: ReleaseTarget) -> dict:
        index = await self.json("https://api.nuget.org/v3/index.json")
        resources = index.get("resources") or []
        def resource(kind: str) -> str:
            return next(r["@id"].rstrip("/") for r in resources if r.get("@type") == kind)
        name = quote(t.name.lower(), safe="")
        version = _nuget_version(t.version) if t.version else None
        base = resource("RegistrationsBaseUrl/3.6.0")
        if version is None:
            content = resource("PackageBaseAddress/3.0.0")
            versions = (await self.json(f"{content}/{name}/index.json")).get("versions") or []
            # The content index is sorted ascending by NuGet version, not by upload time.
            stable = [v for v in versions if "-" not in v]
            if not stable:
                raise ValueError("no stable NuGet release; specify an explicit prerelease version")
            version = stable[-1]
        source = f"{base}/{name}/{quote(version, safe='')}.json"
        leaf = await self.json(source)
        if leaf.get("listed") is False and t.version is None:
            raise ValueError("latest NuGet candidate is unlisted; specify a listed version")
        return {"registry": "nuget", "package": t.name, "version": version, "source": source,
                "published_at": leaf.get("published"), "listed": leaf.get("listed"),
                "resolution": "requested" if t.version else "latest_stable",
                "catalog_entry": leaf.get("catalogEntry")}


def _date(value: str) -> datetime:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return d.astimezone(timezone.utc)


async def check_release(url: str, cfg: Config, client: httpx.AsyncClient, *, now: datetime | None = None) -> CheckResult | None:
    target = target_from_url(url)
    if target is None:
        return None
    hours = cfg.release_cooldown.hours
    detail = {"registry": target.registry, "threshold_hours": hours, "state": "disabled"}
    if hours == 0:
        return CheckResult(name="release_cooldown", status="skip", message="release cooldown disabled", detail=detail)
    now = now or datetime.now(timezone.utc)
    detail.update(state="unknown", checked_at=now.isoformat(), package=target.name or None)
    try:
        async with asyncio.timeout(cfg.net.timeout_s):
            meta = await ReleaseMetadata(client).resolve(target)
        detail.update(meta)
        published = _date(meta["published_at"])
        if published > now or published.year <= 1900:
            raise ValueError("publication timestamp is future-dated or a registry placeholder")
        age = (now - published).total_seconds() / 3600
        remaining = max(0, hours - age)
        active = age < hours
        detail.update(state="active" if active else "elapsed", age_hours=round(age, 6),
                      remaining_hours=round(remaining, 6), published_at=published.isoformat())
        observe(target.registry, True)
        return CheckResult(name="release_cooldown", status="warn" if active else "pass", detail=detail,
                           message=f"{target.registry} {meta['package']} {meta['version']}: release age {age:.2f}h; "
                                   f"cooldown {hours:g}h, remaining {remaining:.2f}h. Age policy only, not a malware scan result.")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        detail.update(state="unknown", error=error)
        if isinstance(exc, (httpx.HTTPError, TimeoutError)):
            observe(target.registry, False, error)
        return CheckResult(name="release_cooldown", status="error", detail=detail,
                           message=f"release cooldown could not be assessed: {error}")


def annotate_result(result: VerifyResult, req: VerifyRequest) -> None:
    """Keep the advisory visible even when the reason writer omits it or a fast path returns."""
    detail = result.checks.get("release_cooldown", {}).get("detail", {})
    state = detail.get("state")
    if state not in ("active", "unknown"):
        return
    # Elapsed hours, not remaining hours or the configured threshold. Match detail precision.
    age_label = f"{detail['age_hours']:.6f}".rstrip("0").rstrip(".") if state == "active" else None
    signal = f"RELEASE_COOLDOWN_PERIOD:{age_label}" if state == "active" else "release_cooldown_unknown"
    if signal not in result.risk_signals:
        result.risk_signals.append(signal)
    chinese = bool(re.search(r"[\u3400-\u9fff]", req.project + req.description))
    if state == "active":
        v, age, hours, left = detail["version"], detail["age_hours"], detail["threshold_hours"], detail["remaining_hours"]
        note = (f"版本 {v} 發布約 {age:.2f} 小時，仍在 {hours:g} 小時冷卻期內，剩餘約 {left:.2f} 小時；建議之後重新查驗。這是發布後觀察期，不代表平台仍在掃描，亦不保證屆時安全。" if chinese else
                f"Version {v} is {age:.2f} hours old and within the {hours:g}-hour release cooldown ({left:.2f} hours remaining); recheck later. This observation period is not a pending scan or a safety guarantee.")
    else:
        note = ("無法確認目標版本的發布時間或冷卻狀態，不能視為已通過冷卻期；詳見 checks.release_cooldown。" if chinese else
                "The target release cooldown could not be assessed; it must not be treated as elapsed. See checks.release_cooldown.")
    note += (" 若要繼續下載或安裝，必須先向使用者說明風險並取得確認；來源驗證通過不代表內容安全。" if chinese else
             " Before proceeding with download or installation, you MUST explain the risk to the user and obtain confirmation; verified origin does not imply safe contents.")
    result.reason += "\n" + note
    result.engine_notes.append(note)
