"""Registry and community statements that a package (or one version of it) is malicious or withdrawn.

- OSV.dev, `MAL-` records only (OpenSSF malicious-packages, which also imports GitHub's malware advisories). A record
  covering every version means the package itself is malware: VERIFIED_FALSE. A record covering specific versions is an
  official package with a compromised release: the verdict (is it official?) is unchanged, but the result says
  DO_NOT_PROCEED for that version (MALICIOUS_VERSION_REPORTED). Withdrawn records are ignored. OSV is asked with the
  exact version, so it decides which records apply.
- PyPI `project-status` (PEP 792, simple API v1): `quarantined` (PyPI's own hold on suspected malware; files are not
  served) is UNVERIFIABLE + PACKAGE_QUARANTINED + DO_NOT_PROCEED; `archived` / `deprecated` are informational notices.

A lookup that fails changes nothing (unknown is neither safe nor malicious). See AGENTS.md §6.5.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..health import observe

OSV_QUERY = "https://api.osv.dev/v1/query"
OSV_ECOSYSTEM = {"pypi": "PyPI", "npm": "npm", "nuget": "NuGet", "go": "Go", "composer": "Packagist"}


def covers_every_version(record: dict[str, Any]) -> bool:
    """True when an OSV record affects all versions of the package: a range introduced at 0 with no upper bound and
    no explicit version list."""
    affected = record.get("affected") or []
    if not affected:
        return False
    for a in affected:
        if a.get("versions") or not a.get("ranges"):
            return False                    # explicit versions, or nothing stated: not "every version"
        for r in a.get("ranges") or []:
            events = r.get("events") or []
            if any("fixed" in e or "last_affected" in e or "limit" in e for e in events):
                return False
            if not any(e.get("introduced") in ("0", "0.0.0") for e in events):
                return False
    return True


def classify_osv(records: list[dict[str, Any]], version_known: bool) -> dict[str, list[str]]:
    """{"whole": [MAL ids covering every version], "version": [MAL ids for this version], "other": [ids, version unknown]}."""
    out: dict[str, list[str]] = {"whole": [], "version": [], "other": []}
    for r in records:
        rid = str(r.get("id") or "")
        if not rid.startswith("MAL-") or r.get("withdrawn"):
            continue
        if covers_every_version(r):
            out["whole"].append(rid)
        elif version_known:
            out["version"].append(rid)       # OSV returned it for the queried version
        else:
            out["other"].append(rid)
    return out


async def check(client: httpx.AsyncClient, package: dict[str, Any], osv: bool = True, pypi_status: bool = True) -> dict[str, Any]:
    """package: {"ecosystem": pypi|npm|nuget|go, "name", "version"}. Returns
    {"block": None|"known_malicious"|"quarantined", "ids": [...], "codes": [...], "notices": [...], "notes": [...]}."""
    eco, name, version = package.get("ecosystem"), package.get("name"), package.get("version")
    out: dict[str, Any] = {"block": None, "ids": [], "codes": [], "notices": [], "notes": [], "package": package}
    if eco not in OSV_ECOSYSTEM or not name:
        return out
    if pypi_status and eco == "pypi":
        st = await _pypi_status(client, name)
        status = st.get("status")
        if st.get("ok") and status == "quarantined":
            out.update(block="quarantined", codes=["PACKAGE_QUARANTINED"])
            out["notes"].append(f"PyPI has quarantined '{name}' (suspected malware; its files are not served)")
            return out
        if st.get("ok") and status in ("archived", "deprecated"):
            out["notices"].append(f"PACKAGE_{status.upper()}")
            out["notes"].append(f"PyPI marks '{name}' as {status}" + (f": {st['reason']}" if st.get("reason") else ""))
        elif not st.get("ok"):
            out["notes"].append(f"PyPI project status unavailable ({st.get('error')})")
    if osv:
        q: dict[str, Any] = {"package": {"name": name, "ecosystem": OSV_ECOSYSTEM[eco]}}
        if version:
            q["version"] = version
        try:
            r = await client.post(OSV_QUERY, json=q)
            r.raise_for_status()
            records = r.json().get("vulns") or []
            observe("osv", True)
        except Exception as e:  # noqa: BLE001
            observe("osv", False, f"{type(e).__name__}: {e}")
            out["notes"].append(f"OSV lookup unavailable ({type(e).__name__}); known-malware check not done")
            return out
        c = classify_osv(records, bool(version))
        if c["whole"]:
            out.update(block="known_malicious", ids=c["whole"], codes=[f"PACKAGE_KNOWN_MALICIOUS:{c['whole'][0]}"])
            out["notes"].append(f"OSV lists '{name}' as malware in every version ({', '.join(c['whole'])})")
            return out
        if c["version"]:
            out["ids"] = c["version"]
            out["notices"].append(f"MALICIOUS_VERSION_REPORTED:{c['version'][0]}")
            out["notes"].append(f"OSV lists {name} {version} as a malicious release ({', '.join(c['version'])}): do not install "
                                "this version; the package itself may still be official")
        if c["other"]:
            out["notes"].append(f"OSV lists malicious releases of '{name}' ({', '.join(c['other'])}); the version to be "
                                "installed is unknown, so they could not be matched")
    return out


async def _pypi_status(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    try:
        r = await client.get(f"https://pypi.org/simple/{name}/", headers={"Accept": "application/vnd.pypi.simple.v1+json"})
        if r.status_code == 404:
            return {"ok": True, "status": None}
        r.raise_for_status()
        ps = r.json().get("project-status") or {}
        return {"ok": True, "status": ps.get("status"), "reason": ps.get("reason")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
