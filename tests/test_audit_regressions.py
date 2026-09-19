"""General counterexamples from the source-verification review; no package-specific exceptions."""
import json
from types import SimpleNamespace

import httpx
import pytest

from urlverify_mcp.config import Config
from urlverify_mcp.evidence import EvidenceStore
from urlverify_mcp.models import CheckResult, Evidence, IdentityGraph, L0Result, LLMSubmission, Verdict
from urlverify_mcp.rules import decide, verify_quotes
from urlverify_mcp.identity.registry import RegistryFastPath
from urlverify_mcp.providers import public_http


def target(url="https://example.org/download", platform=None, owner=None):
    from urlverify_mcp.checks.urltools import etld1_of
    host = httpx.URL(url).host
    return L0Result(normalized_url=url, host=host, etld1=etld1_of(host), platform=platform, platform_owner=owner,
                    checks=[CheckResult(name=n, status="pass") for n in ("scheme", "dns", "public_address", "tls", "redirects")])


def identity_case():
    store = EvidenceStore({"https://techcrunch.com/one": "Example is published at example.org",
                           "https://arstechnica.com/two": "Example is published at example.org"})
    sub = LLMSubmission(identity=IdentityGraph(product="Example", official_domains=["example.org"]),
                        evidence=[Evidence(kind="media", source=u, tier=2, claim="official", quote=t) for u, t in store.items()])
    return store, sub


@pytest.mark.parametrize("source", ["https://techcrunch.com/not-fetched", "https://arstechnica.com/not-fetched"])
def test_quote_cannot_borrow_other_page_or_family(source):
    ev = Evidence(kind="media", source=source, tier=2, claim="official", quote="Example is published at example.org")
    verify_quotes([ev], {"https://techcrunch.com/fetched": ev.quote})
    assert ev.verified_quote is False


def test_structured_one_true_anchor_does_not_validate_fabricated_facts():
    store = EvidenceStore()
    src = "https://www.wikidata.org/wiki/Q1"
    store.record(src, '{"official_website": ["example.org"]}', "wikidata")
    ev = Evidence(kind="wikidata", source=src, tier=1, claim="official sites",
                  quote='"official_website": ["example.org", "forged.example"]')
    verify_quotes([ev], store)
    assert not ev.verified_quote
    ev.quote = '"official_website": ["example.org"]'
    verify_quotes([ev], store)
    assert ev.verified_quote


def test_llm_cannot_label_webpage_as_structured_api():
    store, sub = identity_case()
    u = "https://github.com/pretender/readme"
    store[u] = '{"is_verified": true, "blog": "https://example.org"}'
    sub.identity.official_orgs = {"github": ["pretender"]}
    ev = Evidence(kind="github", source=u, tier=2, claim="official", quote=store[u])
    sub.evidence.append(ev)
    result = decide(Config(), target("https://github.com/pretender/app", "github", "pretender"), sub, store, "app")
    assert result.verdict == Verdict.UNVERIFIABLE
    assert ev.kind == "page" and ev.tier == 3


@pytest.mark.parametrize("name", ["tls", "dns", "public_address", "redirects", "scheme"])
@pytest.mark.parametrize("status", ["error", "skip", None])
def test_incomplete_required_check_cannot_be_true(name, status):
    store, sub = identity_case()
    l0 = target()
    l0.checks = [c for c in l0.checks if c.name != name]
    if status:
        l0.checks.append(CheckResult(name=name, status=status))
    assert decide(Config(), l0, sub, store, "Example").verdict == Verdict.UNVERIFIABLE


def test_redirect_requires_path_owner_even_on_same_platform():
    store, sub = identity_case()
    l0 = target()
    l0.final_url = "https://github.com/unknown/app"
    l0.final_etld1 = "github.com"
    assert decide(Config(), l0, sub, store, "Example").verdict == Verdict.UNVERIFIABLE
    sub.identity.official_orgs = {"github": ["known"]}
    l0 = target("https://github.com/known/app", "github", "known")
    l0.platform_repo = "app"
    l0.final_url = "https://github.com/unknown/app"
    l0.final_etld1 = "github.com"
    assert decide(Config(), l0, sub, store, "app", cached={"official_orgs": {"github": ["known"]}}).verdict == Verdict.UNVERIFIABLE
    l0.final_url = "https://github.com/known/app/releases"
    assert decide(Config(), l0, sub, store, "app", cached={"official_orgs": {"github": ["known"]}}).verdict == Verdict.TRUE


def test_duplicate_family_does_not_increase_confidence():
    store, sub = identity_case()
    initial = decide(Config(), target(), sub, store, "Example")
    assert initial.verdict == Verdict.TRUE
    for i in range(5):
        u = f"https://techcrunch.com/duplicate-{i}"
        store[u] = "Example is published at example.org"
        sub.evidence.append(Evidence(kind="media", source=u, tier=2, claim="official", quote=store[u]))
    assert decide(Config(), target(), sub, store, "Example").confidence == initial.confidence


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "100.64.0.1", "localhost", "printer.local", "::ffff:127.0.0.1"])
async def test_public_address_guard_rejects_nonpublic_without_dns(host, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("local names must not reach DNS")
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden)
    with pytest.raises(public_http.UnsafeURL):
        await public_http.public_addresses(host, 443)


async def test_transport_pins_address_preserves_sni_and_rechecks_dns(monkeypatch):
    requests, pools = [], []
    ip = "8.8.8.8"
    async def addresses(host, port):
        if ip == "127.0.0.1":
            raise public_http.UnsafeURL("rebound")
        return [ip]
    def transport(**kwargs):
        async def respond(req):
            requests.append(req)
            return httpx.Response(200, text="ok")
        instance = httpx.MockTransport(respond)
        pools.append(instance)
        return instance
    monkeypatch.setattr(public_http, "public_addresses", addresses)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    async with public_http.public_client() as client:
        assert (await client.get("https://first.example/a")).text == "ok"
        assert (await client.get("https://second.example/b")).text == "ok"
        assert len(pools) == 2
        assert requests[0].url.host == "8.8.8.8"
        assert requests[0].headers["host"] == "first.example"
        assert requests[0].extensions["sni_hostname"] == "first.example"
        ip = "127.0.0.1"
        with pytest.raises(public_http.UnsafeURL):
            await client.get("https://first.example/again")
        assert len(requests) == 2


async def test_redirect_private_hop_is_never_requested(monkeypatch):
    from urlverify_mcp.checks.redirects import expand
    requests = []
    real_addresses = public_http.public_addresses
    async def addresses(host, port):
        if host == "public.example":
            return ["8.8.8.8"]
        return await real_addresses(host, port)
    def transport(**kwargs):
        def handler(req):
            requests.append(req)
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})
        return httpx.MockTransport(handler)
    monkeypatch.setattr(public_http, "public_addresses", addresses)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    result = await expand("https://public.example/", 1, "test")
    assert result["blocked"] and len(requests) == 1


@pytest.mark.parametrize("registry", ["pypi", "npm"])
@pytest.mark.parametrize("status,expected", [(404, False), (503, None), (429, None)])
async def test_registry_http_failure_is_not_absence(registry, status, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "failure"}))) as client:
        rfp = RegistryFastPath(Config(), SimpleNamespace(client=client))
        assert (await getattr(rfp, registry + "_signals")("example"))["exists"] is expected


async def test_npm_requested_version_and_artifact_do_not_use_latest():
    doc = {"name": "@owner/example", "dist-tags": {"latest": "2.0.0", "old": "1.0.0"},
           "time": {"created": "2020-01-01T00:00:00Z", "1.0.0": "2020-01-01T00:00:00Z", "2.0.0": "2025-01-01T00:00:00Z"},
           "versions": {"1.0.0": {"repository": "https://github.com/owner/old", "dist": {"tarball": "https://registry.npmjs.org/@owner/example/-/example-1.0.0.tgz"}},
                        "2.0.0": {"repository": "https://github.com/owner/new"}}}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=doc))) as client:
        rfp = RegistryFastPath(Config(), SimpleNamespace(client=client))
        for url in ["https://www.npmjs.com/package/@owner/example/v/1.0.0", "https://registry.npmjs.org/@owner/example/old", doc["versions"]["1.0.0"]["dist"]["tarball"]]:
            sig = await rfp.target_signals(target(url, "npm", "@owner/example"))
            assert sig["version"] == "1.0.0" and sig["scope"] == "owner"
            assert sig["repository"].endswith("/old")


async def test_pypi_requested_release_and_exact_file():
    old = {"filename": "example-1.0.tar.gz", "url": "https://files.pythonhosted.org/packages/example-1.0.tar.gz", "yanked": True, "upload_time_iso_8601": "2020-01-01T00:00:00Z"}
    new = {**old, "filename": "example-2.0.whl", "yanked": False}
    def handler(req):
        version = "1.0" if "/1.0/" in req.url.path else "2.0"
        return httpx.Response(200, json={"info": {"name": "example", "version": version}, "urls": [old if version == "1.0" else new], "releases": {"1.0": [old], "2.0": [new]}})
    cfg = Config(); cfg.package_registry_fast_path.typosquat_check = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rfp = RegistryFastPath(cfg, SimpleNamespace(client=client))
        sig = await rfp.target_signals(target("https://pypi.org/project/example/1.0/", "pypi", "example"))
        assert sig["version"] == "1.0" and sig["filename"] == old["filename"] and sig["latest_yanked"]
        sig = await rfp.pypi_signals("example", version="1.0", artifact=old["url"])
        assert sig["exists"] and sig["filename"] == old["filename"]
        assert (await rfp.pypi_signals("example", version="1.0", artifact="https://files.pythonhosted.org/other"))["exists"] is None


async def test_wikidata_records_are_isolated(monkeypatch):
    from urlverify_mcp.agent.loop import Investigator
    async def wikidata(*args):
        return {"ok": True, "entities": [{"source": "https://www.wikidata.org/wiki/Q1", "label": "First"},
                                           {"source": "https://www.wikidata.org/wiki/Q2", "label": "Second"}]}
    inv = Investigator(Config(), None, None, SimpleNamespace(wikidata=wikidata))
    await inv.run_tool("wikidata_lookup", {"name": "test"})
    assert "Second" not in inv.evidence_store["https://www.wikidata.org/wiki/Q1"]
    assert inv.evidence_store.kinds["https://www.wikidata.org/wiki/Q1"] == "wikidata"


def test_provenance_repo_host_must_be_exact():
    from urlverify_mcp.identity.provenance import _owner_repo
    assert _owner_repo("https://evilgithub.com/owner/repo") is None
    assert _owner_repo("https://evil.example/path/github.com/owner/repo") is None
    assert _owner_repo("git+https://github.com/owner/repo.git") == ("owner", "repo")


def test_short_elided_fragment_cannot_inject_owner_vote():
    ev = Evidence(kind="media", source="https://techcrunch.com/x", tier=2, claim="official", quote="Example is released ... evil")
    verify_quotes([ev], {ev.source: "Example is released today"})
    assert not ev.verified_quote


def test_registry_unknown_cannot_borrow_cached_identity():
    store, sub = identity_case()
    assert decide(Config(), target(), sub, store, "Example", registry_state={"state": "unknown"},
                  cached={"official_domains": ["example.org"]}).verdict == Verdict.UNVERIFIABLE


def test_identity_policy_tracks_source_and_allowlist_changes():
    from urlverify_mcp.pipeline import identity_policy
    cfg = Config()
    original = identity_policy(cfg)
    cfg.identity.min_sources += 1
    assert identity_policy(cfg) != original
    cfg.identity.min_sources -= 1
    cfg.lists.denylist.append("example.org")
    assert identity_policy(cfg) != original


async def test_fatal_l0_never_fetches_target(monkeypatch, tmp_path):
    from urlverify_mcp import pipeline
    from urlverify_mcp.storage import Storage
    from urlverify_mcp.models import VerifyRequest
    calls = []
    cfg = Config(prompts={"dir": str(tmp_path / "prompts")})
    l0 = target("https://127.0.0.1/secret")
    l0.checks.append(CheckResult(name="public_address", status="fail", fatal=True))
    async def run_l0(*args, **kwargs): return l0
    class Provider:
        async def fetch(self, url): calls.append(url); return "Example"
        async def close(self): pass
    async def reason(*args): return "rejected"
    monkeypatch.setattr(pipeline, "run_l0", run_l0)
    monkeypatch.setattr(pipeline, "LLM", lambda cfg: None)
    monkeypatch.setattr(pipeline, "make_search_provider", lambda cfg: Provider())
    monkeypatch.setattr(pipeline, "make_fetcher", lambda *args: Provider())
    monkeypatch.setattr(pipeline, "_write_reason", reason)
    result = await pipeline.verify(VerifyRequest(project="Example", url=l0.normalized_url, description="download"), cfg, Storage(tmp_path))
    assert result.verdict == Verdict.FALSE and calls == []


async def test_redirect_loop_and_hop_limit_are_errors(monkeypatch):
    from urlverify_mcp.checks import redirects
    def client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(302, headers={"Location": "/again"})), **kwargs)
    monkeypatch.setattr(redirects, "public_client", client)
    assert (await redirects.expand("https://example.org/", 1, "test"))["error"] == "redirect loop"
    monkeypatch.setattr(redirects, "MAX_HOPS", 1)
    assert (await redirects.expand("https://example.org/", 1, "test"))["error"] == "redirect hop limit exceeded"


async def test_network_address_fixtures():
    from pathlib import Path
    import yaml
    cases = yaml.safe_load((Path(__file__).parent / "fixtures/audit_network.yaml").read_text())["addresses"]
    for case in cases:
        if case["public"]:
            assert await public_http.public_addresses(case["host"], 443) == [case["host"]]
        else:
            with pytest.raises(public_http.UnsafeURL):
                await public_http.public_addresses(case["host"], 443)


@pytest.mark.parametrize("url", ["https://pypi.org/", "https://registry.npmjs.org/", "https://api.nuget.org/v3/index.json"])
async def test_public_transport_tls_online(request, url):
    if not request.config.getoption("--live"):
        pytest.skip("requires --live: public HTTPS transport smoke test (HEAD only)")
    import asyncio
    async with asyncio.timeout(20):
        async with public_http.public_client(timeout=5) as client:
            response = await client.head(url)
            assert response.request.url == httpx.URL(url)
            assert response.status_code < 500


def test_cached_confidence_cap_survives_tls_bonus():
    store, sub = identity_case()
    sub.identity.developer = "Example Corp"
    l0 = target(); l0.tls_org = "Example Corp"
    result = decide(Config(), l0, sub, store, "Example", cached={"official_domains": ["example.org"], "confidence_cap": 0.75})
    assert result.verdict == Verdict.TRUE and result.confidence <= 0.75


async def test_release_resolution_pins_latest_and_is_reused(monkeypatch):
    rfp = RegistryFastPath(Config(), SimpleNamespace(client=None))
    calls = []
    async def npm(name, version=None, artifact=None):
        calls.append(version)
        return {"exists": True, "version": version}
    monkeypatch.setattr(rfp, "npm_signals", npm)
    l0 = target("https://www.npmjs.com/package/example", "npm", "example")
    l0.checks.append(CheckResult(name="release_cooldown", status="warn", detail={"registry": "npm", "package": "example", "version": "1.0.0", "state": "active"}))
    assert (await rfp.target_signals(l0))["version"] == "1.0.0"
    assert (await rfp.registry_state(l0))["version"] == "1.0.0"
    assert calls == ["1.0.0"]
