"""Known malware (OSV MAL- records) and PyPI project status (AGENTS §6.5), with recorded response shapes."""
import json

import httpx

from urlverify_mcp.config import Config
from urlverify_mcp.identity import package_security as ps
from urlverify_mcp.models import CheckResult, IdentityGraph, L0Result, LLMSubmission, Verdict
from urlverify_mcp.rules import decide
from urlverify_mcp.source.run import _next_action

# shapes as returned by api.osv.dev on 2026-10-11
LODAHS_MAL = {"id": "MAL-2025-25502", "affected": [{"package": {"name": "lodahs", "ecosystem": "npm"},
                                                    "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}]}]}]}
LODAHS_GHSA = {"id": "GHSA-hm6q-r2jc-cpqh", "affected": [{"ranges": [{"type": "SEMVER", "events": [{"introduced": "0.0.1"}]}]}]}
DEBUG_MAL = {"id": "MAL-2025-46974", "affected": [{"package": {"name": "debug", "ecosystem": "npm"}, "ranges": [], "versions": ["4.4.2"]}]}
CTX_GHSA = {"id": "GHSA-4g82-3jcr-q52w", "affected": [{"ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"last_affected": "0.1.4"}]}]}]}


def test_whole_package_versus_one_release():
    assert ps.covers_every_version(LODAHS_MAL)
    assert not ps.covers_every_version(DEBUG_MAL)                       # explicit version list
    assert not ps.covers_every_version(CTX_GHSA)                        # bounded range
    assert not ps.covers_every_version({"id": "MAL-x", "affected": [{"package": {}}]})   # nothing stated
    c = ps.classify_osv([LODAHS_MAL, LODAHS_GHSA, DEBUG_MAL, {**LODAHS_MAL, "id": "MAL-old", "withdrawn": "2025-01-01"}], True)
    assert c == {"whole": ["MAL-2025-25502"], "version": ["MAL-2025-46974"], "other": []}   # GHSA and withdrawn ignored
    assert ps.classify_osv([DEBUG_MAL], False)["other"] == ["MAL-2025-46974"]


def _client(osv_vulns=None, pypi_status=None, osv_fail=False):
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url), req.content))
        if req.url.host == "api.osv.dev":
            return httpx.Response(503) if osv_fail else httpx.Response(200, json={"vulns": osv_vulns or []})
        if req.url.host == "pypi.org":
            return httpx.Response(200, json={"name": "x", "project-status": {"status": pypi_status or "active"}})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


async def test_check_outcomes():
    c, seen = _client([LODAHS_MAL])
    r = await ps.check(c, {"ecosystem": "npm", "name": "lodahs", "version": "0.0.1"})
    assert r["block"] == "known_malicious" and r["codes"] == ["PACKAGE_KNOWN_MALICIOUS:MAL-2025-25502"]
    assert json.loads(seen[0][2])["version"] == "0.0.1"                 # OSV is asked about the exact version
    c, _ = _client([DEBUG_MAL])
    r = await ps.check(c, {"ecosystem": "npm", "name": "debug", "version": "4.4.2"})
    assert r["block"] is None and r["notices"] == ["MALICIOUS_VERSION_REPORTED:MAL-2025-46974"]
    c, _ = _client([], pypi_status="quarantined")
    r = await ps.check(c, {"ecosystem": "pypi", "name": "evil", "version": "1.0"})
    assert r["block"] == "quarantined" and r["codes"] == ["PACKAGE_QUARANTINED"]
    c, _ = _client([], pypi_status="archived")
    assert (await ps.check(c, {"ecosystem": "pypi", "name": "old", "version": "1.0"}))["notices"] == ["PACKAGE_ARCHIVED"]
    c, _ = _client(osv_fail=True)
    r = await ps.check(c, {"ecosystem": "npm", "name": "x", "version": "1.0.0"})
    assert r["block"] is None and not r["notices"] and any("OSV lookup unavailable" in n for n in r["notes"])
    c, seen = _client()
    assert (await ps.check(c, {"ecosystem": "cargo", "name": "x"}))["block"] is None and not seen   # not a supported ecosystem


def _l0():
    l0 = L0Result(normalized_url="https://www.npmjs.com/package/lodahs", host="www.npmjs.com", etld1="npmjs.com",
                  platform="npm", platform_scope="user_content", platform_owner="lodahs")
    l0.checks.extend(CheckResult(name=n, status="pass") for n in ("scheme", "dns", "public_address", "tls", "redirects"))
    return l0


def test_rules_and_next_action():
    sub = LLMSubmission(identity=IdentityGraph(product="lodash"))
    d = decide(Config(), _l0(), sub, {}, "lodash", registry_state={"state": "known_malicious", "codes": ["PACKAGE_KNOWN_MALICIOUS:MAL-1"], "notes": ["n"]})
    assert d.verdict == Verdict.FALSE and d.codes == ["PACKAGE_KNOWN_MALICIOUS:MAL-1"]
    q = decide(Config(), _l0(), sub, {}, "lodash", registry_state={"state": "quarantined", "codes": ["PACKAGE_QUARANTINED"], "notes": ["n"]})
    assert q.verdict == Verdict.UNVERIFIABLE and q.codes == ["PACKAGE_QUARANTINED"]
    assert _next_action(Verdict.UNVERIFIABLE, 0.0, ["PACKAGE_QUARANTINED"], []) == "DO_NOT_PROCEED"
    # an official package, but the version to install is a compromised release: TRUE stays, the action does not
    assert _next_action(Verdict.TRUE, 0.9, [], ["MALICIOUS_VERSION_REPORTED:MAL-2025-46974"]) == "DO_NOT_PROCEED"
    assert _next_action(Verdict.TRUE, 0.9, [], ["PACKAGE_ARCHIVED"]) == "PROCEED"


def test_package_identity_from_a_registry_url():
    from urlverify_mcp.pipeline import _package_from_l0
    l0 = _l0()
    assert _package_from_l0(l0) == {"ecosystem": "npm", "name": "lodahs", "version": None}
    l0.checks.append(CheckResult(name="release_cooldown", status="pass", detail={"registry": "npm", "package": "lodahs", "version": "0.0.1"}))
    assert _package_from_l0(l0) == {"ecosystem": "npm", "name": "lodahs", "version": "0.0.1"}
