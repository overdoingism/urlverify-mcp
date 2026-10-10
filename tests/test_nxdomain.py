"""A host that does not exist (NXDOMAIN confirmed over DNS-over-HTTPS) is UNVERIFIABLE with a fix-the-input code and
skips the investigation; a resolver failure that DoH cannot confirm stays an ordinary incomplete check."""
from urlverify_mcp.checks import doh
from urlverify_mcp.config import Config
from urlverify_mcp.models import IdentityGraph, LLMSubmission, Verdict, VerifyResult
from urlverify_mcp.rules import decide


def test_nxdomain_needs_every_answering_resolver_to_say_so():
    assert doh.nxdomain_answer([3, 3]) is True
    assert doh.nxdomain_answer([3]) is True
    assert doh.nxdomain_answer([3, 0]) is False          # one resolver knows the name
    assert doh.nxdomain_answer([]) is None                # nobody answered: unknown, never "does not exist"
    assert doh.nxdomain_answer([2]) is None               # SERVFAIL is not an answer about the name


async def _l0(monkeypatch, tmp_path, confirm):
    from urlverify_mcp import checks
    from urlverify_mcp.storage import Storage

    async def no_dns(host):
        return {"resolved": False, "error": "[Errno -2] Name or service not known"}

    async def fake_confirm(host, resolvers, timeout, ua):
        return {"nxdomain": confirm, "statuses": [3, 3] if confirm else []}
    monkeypatch.setattr(checks.dns_mod, "resolve", no_dns)
    monkeypatch.setattr(checks.doh_mod, "confirm_nxdomain", fake_confirm)
    return await checks.run_l0("https://lmstudio-download.com/", Config(), Storage(tmp_path), known_official=["lmstudio.ai"])


async def test_confirmed_missing_host_is_unverifiable_and_fixable(monkeypatch, tmp_path):
    l0 = await _l0(monkeypatch, tmp_path, True)
    dns = next(c for c in l0.checks if c.name == "dns")
    assert dns.status == "fail" and not dns.fatal and l0.target_missing and not l0.fatal_failures
    d = decide(Config(), l0, LLMSubmission(identity=IdentityGraph(product="LM Studio")), {}, "LM Studio")
    assert d.verdict == Verdict.UNVERIFIABLE and d.codes == ["TARGET_DOMAIN_DOES_NOT_EXIST"], d.notes
    from urlverify_mcp.source.run import _next_action, result_codes
    res = VerifyResult(verdict=d.verdict, confidence=0.0, reason="x", rule_codes=d.codes,
                       checks={c.name: {"status": c.status, "fatal": c.fatal, "message": c.message, "detail": c.detail} for c in l0.checks},
                       established_edges=d.established_edges, missing_edges=d.missing_edges)
    codes, notices = result_codes(res)
    assert codes == ["TARGET_DOMAIN_DOES_NOT_EXIST"]                       # no MISSING_EDGE noise
    assert _next_action(res.verdict, 0.0, codes, notices) == "FIX_INPUT_AND_RETRY"


async def test_unconfirmed_dns_failure_stays_incomplete(monkeypatch, tmp_path):
    l0 = await _l0(monkeypatch, tmp_path, None)
    dns = next(c for c in l0.checks if c.name == "dns")
    assert dns.status == "error" and not l0.target_missing
    d = decide(Config(), l0, LLMSubmission(identity=IdentityGraph(product="LM Studio")), {}, "LM Studio")
    assert d.verdict == Verdict.UNVERIFIABLE and "TARGET_DOMAIN_DOES_NOT_EXIST" not in d.codes
