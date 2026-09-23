"""Fixed, gap-driven lookups (AGENTS §6.3) with canned structured-tool answers: which lookups run, which Wikidata
entities are accepted, and when the rules can decide without the LLM."""
from urlverify_mcp.config import Config
from urlverify_mcp.evidence import EvidenceStore
from urlverify_mcp.identity.prefetch import Prefetch, entity_accepted, merge
from urlverify_mcp.models import CheckResult, Evidence, IdentityGraph, L0Result, LLMSubmission, Verdict
from urlverify_mcp.rules import decide

STABLE = {"ok": True, "stable": True, "recent_change": False}


class FakeStructured:
    def __init__(self, wikidata=None, github=None, wikipedia=None):
        self.wd, self.gh, self.wp = wikidata or {}, github or {}, wikipedia or {}
        self.calls = []

    async def wikidata(self, name, *a):
        self.calls.append(("wikidata", name))
        return {"ok": True, "found": True, "entities": self.wd.get(name, [])}

    async def wikipedia_history(self, title, *a):
        self.calls.append(("wikipedia", title))
        return self.wp.get(title, {"ok": True, "found": False})

    async def github(self, owner, repo=None):
        self.calls.append(("github", owner, repo))
        return self.gh.get((owner, repo), {"ok": False, "error": "HTTP 404"})

    async def huggingface(self, owner, repo=None):
        return {"ok": False, "error": "HTTP 404"}


def l0_for(host, platform=None, owner=None, repo=None, scope=None):
    from urlverify_mcp.checks.urltools import etld1_of
    l0 = L0Result(normalized_url=f"https://{host}/x", host=host, etld1=etld1_of(host), platform=platform,
                  platform_scope=scope or ("user_content" if platform else None), platform_owner=owner, platform_repo=repo)
    for n in ("scheme", "public_address", "dns", "tls", "redirects"):
        l0.checks.append(CheckResult(name=n, status="pass"))
    return l0


LLAMA_WD = {"qid": "Q125998452", "label": "llama.cpp", "aliases": [], "official_website": [],
            "official_repos": ["github.com/ggerganov/llama.cpp", "github.com/ggml-org/llama.cpp"],
            "repo_stability": STABLE, "developer": [], "enwiki": None, "source": "https://www.wikidata.org/wiki/Q125998452"}
GH_GGML = {"ok": True, "owner": "ggml-org", "owner_info": {"login": "ggml-org", "type": "Organization", "blog": "https://ggml.ai", "is_verified": True},
           "repo_info": {"full_name": "ggml-org/llama.cpp", "fork": False}, "source": "https://github.com/ggml-org/llama.cpp"}


def test_entity_acceptance_is_exact():
    l0 = l0_for("desktop.docker.com", "dockerhub", scope="company_site")
    assert entity_accepted({"label": "Docker", "official_website": ["https://www.docker.com"]}, "Docker Desktop", l0)
    assert entity_accepted({"label": "Docker Desktop"}, "docker desktop", l0)
    assert not entity_accepted({"label": "Docker (film)", "official_website": ["https://example.org"]}, "Docker Desktop", l0)
    gh = l0_for("github.com", "github", "ggml-org", "llama.cpp")
    assert entity_accepted({"label": "x", "official_repos": ["github.com/ggml-org/llama.cpp"]}, "zzz", gh)
    assert not entity_accepted({"label": "x", "official_repos": ["github.com/ggml-org-evil/llama.cpp"]}, "zzz", gh)


async def test_platform_target_decided_without_llm():
    fs = FakeStructured(wikidata={"llama.cpp": [LLAMA_WD]}, github={("ggml-org", "llama.cpp"): GH_GGML})
    store = EvidenceStore()
    l0 = l0_for("github.com", "github", "ggml-org", "llama.cpp")
    sub = await Prefetch(Config(), fs, store).run(l0, "llama.cpp")
    assert ("github", "ggml-org", "llama.cpp") in fs.calls
    d = decide(Config(), l0, sub, store, "llama.cpp")
    assert d.verdict == Verdict.TRUE and "PROJECT_TO_ORG:github:ggml-org" in d.established_edges, d.notes


async def test_fork_owner_is_not_decided_true():
    """drluoto/llama.cpp: Wikimedia names ggml-org; the fork owner never inherits it."""
    fork = {"ok": True, "owner": "drluoto", "owner_info": {"login": "drluoto", "type": "User"},
            "repo_info": {"full_name": "drluoto/llama.cpp", "fork": True, "parent": "ggml-org/llama.cpp"},
            "source": "https://github.com/drluoto/llama.cpp"}
    gh_org = {**GH_GGML, "repo_info": None, "source": "https://github.com/ggml-org"}
    fs = FakeStructured(wikidata={"llama.cpp": [LLAMA_WD]}, github={("drluoto", "llama.cpp"): fork, ("ggml-org", None): gh_org})
    store = EvidenceStore()
    l0 = l0_for("github.com", "github", "drluoto", "llama.cpp")
    sub = await Prefetch(Config(), fs, store).run(l0, "llama.cpp")
    assert ("github", "ggml-org", None) in fs.calls                      # the owner Wikimedia names gets its record too
    assert not any("parent" in e.quote for e in sub.evidence)
    d = decide(Config(), l0, sub, store, "llama.cpp")
    assert d.verdict == Verdict.FALSE, d.notes                           # decided without the LLM


async def test_no_match_reports_gap_and_is_not_decisive():
    fs = FakeStructured()
    store = EvidenceStore()
    l0 = l0_for("github.com", "github", "someone", "tool")
    pre = Prefetch(Config(), fs, store)
    sub = await pre.run(l0, "tool")
    assert "WIKIMEDIA_NO_MATCH" in pre.codes and [c[1] for c in fs.calls if c[0] == "wikidata"] == ["tool"]  # same name not repeated
    d = decide(Config(), l0, sub, store, "tool")
    assert d.verdict == Verdict.UNVERIFIABLE and any(m["edge"].startswith("PROJECT_TO_ORG") for m in d.missing_edges)


async def test_developer_is_the_next_candidate_and_queries_are_capped():
    dev_ent = {"qid": "Q1", "label": "Element Labs", "official_website": ["https://lmstudio.ai"], "source": "https://www.wikidata.org/wiki/Q1"}
    prod = {"qid": "Q2", "label": "LM Studio", "official_website": [], "developer": [{"label": "Element Labs", "official_website": ["https://lmstudio.ai"]}],
            "source": "https://www.wikidata.org/wiki/Q2"}
    fs = FakeStructured(wikidata={"LM Studio": [prod], "Element Labs": [dev_ent]})
    pre = Prefetch(Config(identity={"homebrew_reverse_lookup": False}), fs, EvidenceStore())   # no network in unit tests
    sub = await pre.run(l0_for("lmstudio.ai"), "LM Studio")
    assert [c[1] for c in fs.calls if c[0] == "wikidata"] == ["LM Studio", "Element Labs"]
    assert "lmstudio.ai" in sub.identity.official_domains and pre.wikimedia_queries <= 3


def test_merge_keeps_llm_prose_and_unions_candidates():
    det = LLMSubmission(identity=IdentityGraph(product="p", official_domains=["a.org"], official_orgs={"github": ["o1"]}),
                        evidence=[Evidence(kind="wikidata", source="https://www.wikidata.org/wiki/Q1", claim="c", facts=["F1"])])
    llm = LLMSubmission(identity=IdentityGraph(product="p", developer="Dev", official_domains=["b.org"], official_orgs={"github": ["O1", "o2"]}),
                        evidence=[Evidence(kind="media", source="https://news.example/x", claim="c", quote="q")], proposed_verdict="VERIFIED_TRUE")
    m = merge(det, llm)
    assert m.identity.official_domains == ["b.org", "a.org"] and m.identity.official_orgs["github"] == ["O1", "o2"]
    assert len(m.evidence) == 2 and m.identity.developer == "Dev" and m.proposed_verdict == "VERIFIED_TRUE"


def test_false_carries_native_reason_code():
    import asyncio
    fork = {"ok": True, "owner": "drluoto", "owner_info": {"login": "drluoto", "type": "User"},
            "repo_info": {"full_name": "drluoto/llama.cpp", "fork": True}, "source": "https://github.com/drluoto/llama.cpp"}
    gh_org = {**GH_GGML, "repo_info": None, "source": "https://github.com/ggml-org"}
    fs = FakeStructured(wikidata={"llama.cpp": [LLAMA_WD]}, github={("drluoto", "llama.cpp"): fork, ("ggml-org", None): gh_org})
    store = EvidenceStore()
    l0 = l0_for("github.com", "github", "drluoto", "llama.cpp")
    sub = asyncio.run(Prefetch(Config(), fs, store).run(l0, "llama.cpp"))
    d = decide(Config(), l0, sub, store, "llama.cpp")
    assert d.verdict == Verdict.FALSE and d.codes == ["OWNER_NOT_OFFICIAL"], (d.codes, d.notes)


def test_brew_index_and_acceptance():
    from urlverify_mcp.identity.brew_index import accepted, build_index
    idx = build_index([
        {"token": "docker-desktop", "name": ["Docker Desktop"], "homepage": "https://www.docker.com/products/docker-desktop",
         "url": "https://desktop.docker.com/mac/main/arm64/1/Docker.dmg"},
        {"token": "some-tool", "name": ["Some Tool"], "homepage": "https://elsewhere.example", "url": "https://desktop.docker.com/x.dmg"},
        {"token": "gh-app", "name": ["GH App"], "homepage": "https://gh.example",
         "url": "https://github.com/o/r/releases/download/v1/a.dmg", "variations": {"sonoma": {"url": "https://cdn.example/b.dmg"}}}])
    assert [e["token"] for e in idx["docker.com"]] == ["docker-desktop", "some-tool"]
    assert "cdn.example" in idx and "github.com" in idx
    got = [e["token"] for e in idx["docker.com"] if accepted(e, "Docker Desktop", "docker.com")]
    assert got == ["docker-desktop"]                     # homepage elsewhere and a different name: not taken
    assert accepted(idx["docker.com"][0], "Something Else", "docker.com")    # homepage on the same domain


async def test_brew_reverse_lookup_in_prefetch(tmp_path, monkeypatch):
    import json as _json
    import httpx as _httpx
    from urlverify_mcp.identity import brew_index
    cat = [{"token": "docker-desktop", "name": ["Docker Desktop"], "homepage": "https://www.docker.com/products/docker-desktop",
            "url": "https://desktop.docker.com/mac/main/arm64/1/Docker.dmg"}]
    rec = _json.dumps(cat[0], separators=(",", ":"))

    class FakeClient:
        async def get(self, url, **kw):
            return _httpx.Response(200, text=rec, request=_httpx.Request("GET", url))

    async def fake_index(self):
        return brew_index.build_index(cat)
    monkeypatch.setattr(brew_index.BrewCaskIndex, "index", fake_index)
    fs = FakeStructured()
    fs.client = FakeClient()
    cfg = Config(storage={"dir": str(tmp_path)})
    store = EvidenceStore()
    l0 = l0_for("desktop.docker.com", "dockerhub", scope="company_site")
    sub = await Prefetch(cfg, fs, store).run(l0, "Docker Desktop")
    ev = [e for e in sub.evidence if e.kind == "distro"]
    assert len(ev) == 1 and '"homepage":"https://www.docker.com/products/docker-desktop"' in ev[0].quote
    from urlverify_mcp.rules import verify_quotes
    verify_quotes(ev, store)
    assert ev[0].verified_quote


class FakeFlathub(FakeStructured):
    def __init__(self, apps, **kw):
        super().__init__(**kw)
        self.apps = apps

    async def flathub(self, app_id):
        return self.apps.get(app_id, {"ok": True, "found": False, "source": f"https://flathub.org/apps/{app_id}"})


def _fh(app_id, verified, website=None, name=None):
    info = {"name": name or app_id.split(".")[-1], "is_verified": verified, "verification_method": "website" if website else "none"}
    if website:
        info["website"] = website
    return {"ok": True, "found": True, "owner": app_id, "owner_info": info, "repo_info": {"id": app_id},
            "source": f"https://flathub.org/apps/{app_id}"}


async def test_flathub_verified_app_is_established_by_its_verified_domain():
    obs_wd = {"qid": "Q1", "label": "OBS Studio", "official_website": ["https://obsproject.com"],
              "stability": STABLE, "source": "https://www.wikidata.org/wiki/Q1"}
    fs = FakeFlathub({"com.obsproject.Studio": _fh("com.obsproject.Studio", True, "https://obsproject.com", name="OBS Studio")},
                     wikidata={"OBS Studio": [obs_wd]})
    store = EvidenceStore()
    l0 = l0_for("flathub.org", "flathub", "com.obsproject.Studio")
    pre = Prefetch(Config(), fs, store)
    sub = await pre.run(l0, "OBS Studio")
    d = decide(Config(), l0, sub, store, "OBS Studio")
    assert d.verdict == Verdict.TRUE and "PROJECT_TO_ORG:flathub:com.obsproject.studio" in d.established_edges, d.notes


async def test_flathub_unverified_app_is_not_true_and_says_why():
    vlc_wd = {"qid": "Q2", "label": "VLC media player", "aliases": ["VLC"], "official_website": ["https://www.videolan.org/vlc/"],
              "stability": STABLE, "source": "https://www.wikidata.org/wiki/Q2"}
    fs = FakeFlathub({"org.videolan.VLC": _fh("org.videolan.VLC", False)}, wikidata={"VLC": [vlc_wd]})
    store = EvidenceStore()
    l0 = l0_for("flathub.org", "flathub", "org.videolan.VLC")
    pre = Prefetch(Config(), fs, store)
    sub = await pre.run(l0, "VLC")
    assert "FLATHUB_UNVERIFIED" in pre.codes
    assert not any('"website"' in (store.get(e.source) or "") for e in sub.evidence if e.kind == "flathub")
    d = decide(Config(), l0, sub, store, "VLC")
    assert d.verdict == Verdict.UNVERIFIABLE, d.notes


async def test_flathub_missing_app():
    fs = FakeFlathub({})
    pre = Prefetch(Config(), fs, EvidenceStore())
    await pre.run(l0_for("flathub.org", "flathub", "org.nope.Nothing"), "Nothing")
    assert "FLATHUB_APP_NOT_FOUND" in pre.codes



def test_display_name_only_from_the_targets_own_record():
    """A different app's record naming "OBS Studio" does not satisfy the name match for com.evil.Studio."""
    import json as _json
    store = EvidenceStore()
    rec = _fh("com.evil.Studio", True, "https://evil.example", name="Evil Studio")
    other = _fh("com.obsproject.Studio", True, "https://obsproject.com", name="OBS Studio")
    store.record(rec["source"], _json.dumps(rec), "flathub")
    store.record(other["source"], _json.dumps(other), "flathub")
    ev = [Evidence(kind="flathub", source=src, claim="c", facts=[f for f, (s2, _, _) in store.facts.items() if s2 == src])
          for src in (rec["source"], other["source"])]
    from urlverify_mcp.rules import _project_matches, verify_quotes
    verify_quotes(ev, store)
    l0 = l0_for("flathub.org", "flathub", "com.evil.Studio")
    assert _project_matches("OBS Studio", l0, ev)[0] is False
    assert _project_matches("Evil Studio", l0, ev)[0] is True


def test_unverified_flathub_app_not_established_by_flathub_family_votes():
    """VLC regression: Flathub record + Flathub's own GitHub build manifest + one aged blog must NOT make an unverified
    app official. The manifest repo is the Flathub family; unverified apps get no Flathub-family support at all."""
    import json as _json
    from urlverify_mcp.identity.sources import family_of, source_key
    assert family_of(source_key("https://github.com/flathub/org.videolan.VLC")) == "flathub"
    assert family_of(source_key("https://raw.githubusercontent.com/Homebrew/homebrew-cask/master/Casks/d/docker.rb")) == "brew.sh"
    store = EvidenceStore()
    fh = _fh("org.videolan.VLC", False, name="VLC")
    store.record(fh["source"], _json.dumps(fh), "flathub")
    gh = {"ok": True, "owner": "flathub", "owner_info": {"login": "flathub"}, "repo_info": {"full_name": "flathub/org.videolan.VLC"},
          "source": "https://github.com/flathub/org.videolan.VLC"}
    store.record(gh["source"], _json.dumps(gh), "github")
    blog = "https://www.fosslinux.com/1/top-apps"
    store[blog] = "fosslinux@tuts:~$ flatpak install flathub org.videolan.VLC"
    ev = [Evidence(kind="flathub", source="(facts)", claim="c", facts=[f for f, (s2, _, _) in store.facts.items() if s2 == fh["source"]]),
          Evidence(kind="github", source="(facts)", claim="c", facts=[f for f, (s2, _, _) in store.facts.items() if s2 == gh["source"]]),
          Evidence(kind="media", source=blog, tier=2, claim="c", quote="flatpak install flathub org.videolan.VLC")]
    ident = IdentityGraph(product="VLC", official_orgs={"flathub": ["org.videolan.VLC"]})
    l0 = l0_for("flathub.org", "flathub", "org.videolan.VLC")
    ages = {blog: {"ok": True, "method": "wayback", "strength": "strong", "created_ts": 1.0, "age_days": 700}}
    d = decide(Config(), l0, LLMSubmission(identity=ident, evidence=ev), store, "VLC", ages=ages)
    assert d.verdict == Verdict.UNVERIFIABLE, d.notes
    assert any("community packaging" in n for n in d.notes)
