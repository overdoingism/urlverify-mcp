"""Typed facts: structured records are flattened into numbered facts the LLM cites by id; pages and records from the
same URL never overwrite each other; a record can be cited by any of its URLs."""
import json

from urlverify_mcp.config import Config
from urlverify_mcp.evidence import EvidenceStore, norm_url
from urlverify_mcp.models import CheckResult, Evidence, IdentityGraph, L0Result, LLMSubmission, Verdict
from urlverify_mcp.rules import decide, verify_quotes

GH = {"ok": True, "owner_info": {"login": "ggml-org", "is_verified": True, "blog": "https://ggml.ai"},
      "repo": {"full_name": "ggml-org/llama.cpp", "fork": False, "homepage": "https://llama.app"},
      "source": "https://github.com/ggml-org/llama.cpp"}


def _store():
    st = EvidenceStore()
    st.record(GH["source"], json.dumps(GH), "github", aliases=["https://api.github.com/repos/ggml-org/llama.cpp", "https://github.com/ggml-org"])
    return st


def test_flatten_and_render():
    st = _store()
    text = st.render(GH["source"], "github_info(owner=ggml-org, repo=llama.cpp)")
    assert "RECORD R1" in text and "github.repo.full_name = ggml-org/llama.cpp" in text and '"facts"' in text
    assert not any(p.endswith(".source") or p.endswith(".ok") for _, p, _ in st.facts.values())


def test_page_and_record_for_same_url_coexist():
    st = _store()
    st["https://github.com/ggml-org/llama.cpp"] = "llama.cpp README: LLM inference in C/C++"
    assert st.kinds[GH["source"]] == "github" and '"full_name"' in st[GH["source"]]
    assert st.page_text("https://github.com/ggml-org/llama.cpp/") == "llama.cpp README: LLM inference in C/C++"


def test_fact_citation_rewrites_source_and_quote():
    st = _store()
    fid = next(f for f, (_, p, _) in st.facts.items() if p == "github.owner_info.login")
    ev = Evidence(kind="media", source="(facts)", claim="ggml-org is the org", facts=[fid, "F999"])
    verify_quotes([ev], st)
    assert ev.verified_quote and ev.source == GH["source"] and ev.kind == "github"
    assert ev.quote == "github.owner_info.login = ggml-org; github.repo.full_name = ggml-org/llama.cpp" or \
        ev.quote.startswith("github.owner_info.login = ggml-org"), ev.quote
    assert any("F999" in n for n in ev.notes)
    bad = Evidence(kind="github", source="(facts)", claim="x", facts=["F404"])
    verify_quotes([bad], st)
    assert bad.verified_quote is False


def test_record_cited_by_api_alias():
    st = _store()
    ev = Evidence(kind="github", source="https://api.github.com/repos/ggml-org/llama.cpp", claim="ggml-org owns llama.cpp",
                  quote='"full_name": "ggml-org/llama.cpp"')
    verify_quotes([ev], st)
    assert ev.verified_quote and ev.source == GH["source"]
    assert norm_url("https://api.github.com/users/Foo") == "github.com/foo"


def test_llama_cpp_org_established_from_facts():
    """The regression that motivated typed facts: Wikidata P1324 + GitHub record = two families for ggml-org."""
    st = _store()
    wd = {"qid": "Q125998452", "label": "llama.cpp", "official_repos": ["github.com/ggml-org/llama.cpp"],
          "repo_stability": {"stable": True, "recent_change": False, "current": ["github.com/ggml-org/llama.cpp"],
                             "value_days_ago": ["github.com/ggml-org/llama.cpp"]}, "source": "https://www.wikidata.org/wiki/Q125998452"}
    st.record(wd["source"], json.dumps(wd), "wikidata")
    f_login = next(f for f, (_, p, _) in st.facts.items() if p == "github.owner_info.login")
    f_repo = next(f for f, (_, p, _) in st.facts.items() if p.startswith("wikidata.official_repos"))
    sub = LLMSubmission(identity=IdentityGraph(product="llama.cpp", official_orgs={"github": ["ggml-org"]}), proposed_verdict="VERIFIED_TRUE",
                        evidence=[Evidence(kind="github", source="(facts)", claim="org", facts=[f_login]),
                                  Evidence(kind="wikidata", source="(facts)", claim="repo", facts=[f_repo])])
    l0 = L0Result(normalized_url="https://github.com/ggml-org/llama.cpp/releases/download/b1/x.zip", host="github.com", etld1="github.com",
                  platform="github", platform_scope="user_content", platform_owner="ggml-org", platform_repo="llama.cpp")
    for n in ("scheme", "public_address", "dns", "tls", "redirects"):
        l0.checks.append(CheckResult(name=n, status="pass"))
    d = decide(Config(), l0, sub, st, "llama.cpp")
    assert d.verdict == Verdict.TRUE, d.notes


def test_store_roundtrip():
    st = _store()
    st["https://x.example/p"] = "page"
    back = EvidenceStore.from_json(json.loads(json.dumps(st.to_json())))
    assert back.kinds == st.kinds and back.facts == st.facts and back.page_text("https://x.example/p") == "page"
    assert back.find_record("https://api.github.com/repos/ggml-org/llama.cpp") == GH["source"]


def test_identity_facts_always_attached():
    """drluoto regression: citing only `fullname` / `num_models` of a Hugging Face account still says whose account it is."""
    st = EvidenceStore()
    hf = {"owner": "drluoto", "owner_info": {"type": "user", "name": "drluoto", "fullname": "Johannes Luoto", "num_models": 2}}
    st.record("https://huggingface.co/drluoto", json.dumps(hf), "huggingface")
    fid = next(f for f, (_, p, _) in st.facts.items() if p.endswith("fullname"))
    ev = Evidence(kind="huggingface", source="(facts)", claim="same person", facts=[fid])
    verify_quotes([ev], st)
    assert "huggingface.owner = drluoto" in ev.quote and "Johannes Luoto" in ev.quote
