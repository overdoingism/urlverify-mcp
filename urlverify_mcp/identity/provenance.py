"""Build provenance: the registry's own signed statement of *which repository's CI published this package*.
npm: Sigstore attestations (SLSA v1) at registry.npmjs.org/-/npm/v1/attestations/<name>@<version>.
PyPI: PEP 740 provenance at pypi.org/integrity/<project>/<version>/<file>/provenance (Trusted Publishing).
deps.dev (Google Open Source Insights) is consulted as an independent verifier of the same fact.

A name-squatter cannot produce an attestation naming someone else's repository: that requires running CI inside that
repository. This is the package world's verified badge, and the only signal that answers "who published it?"."""
from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..health import observe

GH_RE = re.compile(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)")


def _owner_repo(url: str | None) -> tuple[str, str] | None:
    m = GH_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else None


class Provenance:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def npm(self, name: str, version: str | None) -> dict[str, Any]:
        """Returns {"ok", "found", "repo": (owner, repo), "workflow", "source": url} or an error."""
        if not version:
            return {"ok": True, "found": False, "error": "no version"}
        url = f"https://registry.npmjs.org/-/npm/v1/attestations/{quote(name, safe='')}@{version}"
        try:
            r = await self.client.get(url)
            if r.status_code == 404:
                observe("npm", True)
                return {"ok": True, "found": False, "source": url}
            r.raise_for_status()
            observe("npm", True)
        except Exception as e:  # noqa: BLE001
            observe("npm", False, f"attestations: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        for att in r.json().get("attestations", []):
            if "slsa.dev/provenance" not in str(att.get("predicateType", "")):
                continue
            try:
                payload = json.loads(base64.b64decode(att["bundle"]["dsseEnvelope"]["payload"]))
            except Exception:  # noqa: BLE001
                continue
            pred = payload.get("predicate", {})
            ws = pred.get("buildDefinition", {}).get("externalParameters", {}).get("workflow", {})
            repo_url = ws.get("repository") or pred.get("invocation", {}).get("configSource", {}).get("uri")
            pair = _owner_repo(repo_url)
            if pair:
                return {"ok": True, "found": True, "repo": pair, "repo_url": repo_url, "ref": ws.get("ref"),
                        "workflow": ws.get("path"), "builder": (pred.get("runDetails", {}).get("builder") or {}).get("id"),
                        "source": url, "kind": "npm-provenance"}
        return {"ok": True, "found": False, "source": url}

    async def pypi(self, project: str, version: str | None, filename: str | None) -> dict[str, Any]:
        if not version or not filename:
            return {"ok": True, "found": False, "error": "no version/file"}
        url = f"https://pypi.org/integrity/{quote(project)}/{quote(version)}/{quote(filename)}/provenance"
        try:
            r = await self.client.get(url)
            if r.status_code in (404, 403):
                observe("pypi", True)
                return {"ok": True, "found": False, "source": url}
            r.raise_for_status()
            observe("pypi", True)
        except Exception as e:  # noqa: BLE001
            observe("pypi", False, f"provenance: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        for b in r.json().get("attestation_bundles", []):
            pub = b.get("publisher", {})
            if pub.get("kind") == "GitHub" and pub.get("repository"):
                pair = _owner_repo("https://github.com/" + pub["repository"])
                if pair:
                    return {"ok": True, "found": True, "repo": pair, "repo_url": "https://github.com/" + pub["repository"],
                            "workflow": pub.get("workflow"), "source": url, "kind": "pypi-provenance"}
        return {"ok": True, "found": False, "source": url}

    async def depsdev(self, system: str, name: str, version: str | None) -> dict[str, Any]:
        """Independent corroboration: deps.dev verifies SLSA provenance itself and names the source repository."""
        if not version:
            return {"ok": True, "found": False}
        url = f"https://api.deps.dev/v3/systems/{system}/packages/{quote(name, safe='')}/versions/{quote(version, safe='')}"
        try:
            r = await self.client.get(url)
            if r.status_code == 404:
                observe("deps.dev", True)
                return {"ok": True, "found": False, "source": url}
            r.raise_for_status()
            observe("deps.dev", True)
        except Exception as e:  # noqa: BLE001
            observe("deps.dev", False, f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        j = r.json()
        for p in j.get("slsaProvenances", []) or []:
            pair = _owner_repo(p.get("sourceRepository"))
            if pair and p.get("verified"):
                return {"ok": True, "found": True, "repo": pair, "repo_url": p.get("sourceRepository"), "verified": True,
                        "source": url, "kind": "deps.dev"}
        return {"ok": True, "found": False, "source": url}
