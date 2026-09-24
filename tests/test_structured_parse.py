from urlverify_mcp.identity.structured import _infobox_repos, _infobox_sites, _norm_repo, _stability_verdict


def test_norm_repo():
    assert _norm_repo("https://GitHub.com/ggml-org/llama.cpp.git/") == "github.com/ggml-org/llama.cpp"
    assert _norm_repo("github.com/ggml-org/llama.cpp") == "github.com/ggml-org/llama.cpp"
    assert _norm_repo("https://www.github.com/Org/Repo?tab=readme") == "github.com/org/repo"
    assert _norm_repo("") is None


def test_infobox_repo_field_only():
    wikitext = ("{{Infobox software\n| name = llama.cpp\n| repo = {{URL|github.com/ggml-org/llama.cpp}}"
                "<ref>{{cite web |url=https://github.com/evil/llama.cpp |title=x}}</ref>\n| developer = [[Georgi Gerganov]]\n}}\n"
                "Body text linking https://github.com/someone/else must not count.\n")
    assert _infobox_repos(wikitext) == ["github.com/ggml-org/llama.cpp"]
    assert _infobox_sites(wikitext) == []


def test_repo_stability_uses_same_verdict_helper():
    vals = [{"ts": "t", "official": ["github.com/ggml-org/llama.cpp"]}] * 3
    v = _stability_verdict(vals, ["github.com/ggml-org/llama.cpp"], 3, 90)
    assert v["stable"] and not v["recent_change"]
    v = _stability_verdict(vals, ["github.com/ggerganov/llama.cpp"], 3, 90)
    assert v["recent_change"] and not v["stable"]


def test_self_closing_ref_does_not_swallow_infobox():
    wikitext = ('{{Infobox software\n| license = [[MIT License]]<ref name="license"/>\n| repo = {{URL|github.com/ggml-org/llama.cpp}}\n'
                '| website = {{URL|https://example.org}}\n}}\nText<ref name="a">{{cite web |url=https://evil.example}}</ref> more.\n')
    assert _infobox_repos(wikitext) == ["github.com/ggml-org/llama.cpp"]
    assert _infobox_sites(wikitext) == ["example.org"]


def test_infobox_website_shown_from_wikidata():
    from urlverify_mcp.identity.structured import _infobox_sites, _infobox_uses_wikidata
    omitted = "{{Infobox software\n| name = 7-Zip\n| license = x<ref>{{cite web | url = https://7-zip.org/l.txt | website = 7-zip.org}}</ref>\n}}"
    assert _infobox_uses_wikidata(omitted) and _infobox_sites(omitted) == []          # citation fields are not the infobox's
    assert _infobox_uses_wikidata("{{Infobox software\n| website = {{Official URL}}\n}}")
    assert not _infobox_uses_wikidata("{{Infobox software\n| website = hide\n}}")
    assert not _infobox_uses_wikidata("{{Infobox company\n| name = x\n}}")                # no verified Wikidata fallback
    assert _infobox_sites("{{Infobox software\n| website = 7-zip.org\n}}") == ["7-zip.org"]   # bare domain
