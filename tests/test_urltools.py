from urlverify_mcp.checks.urltools import analyse_host, etld1_of, normalize_url, skeleton


def test_normalize_and_etld1():
    assert normalize_url("LMStudio.ai/download") == "https://lmstudio.ai/download"
    assert etld1_of("objects.githubusercontent.com") == "githubusercontent.com"
    assert etld1_of("foo.bar.co.uk") == "bar.co.uk"


def test_homoglyph_cyrillic_i():
    host = normalize_url("https://lmstudіo.ai/").split("/")[2]   # cyrillic і
    assert host.startswith("xn--")
    f = analyse_host(host, ["lmstudio.ai"])
    assert f["punycode"] and f["confusable_of"] == "lmstudio.ai"


def test_subdomain_abuse_and_brand_in_label():
    f = analyse_host("lmstudio.ai.evil-cdn.net", ["lmstudio.ai"])
    assert f["subdomain_abuse_of"] == "lmstudio.ai"
    f2 = analyse_host("lmstudio-download.com", ["lmstudio.ai"])
    assert f2.get("brand_in_label_of") == "lmstudio.ai"


def test_typosquat():
    f = analyse_host("huggingfase.co", ["huggingface.co"])
    assert f["typosquat_of"] == "huggingface.co"
    assert analyse_host("huggingface.co", ["huggingface.co"])["typosquat_of"] is None


def test_skeleton():
    assert skeleton("rnicrosoft") == "microsoft"


def test_clean_url_strips_llm_annotations():
    from urlverify_mcp.agent.loop import _clean_url
    assert _clean_url("https://registry.npmjs.org/@electron/asar (via package_registry npm)") == "https://registry.npmjs.org/@electron/asar"
    assert _clean_url("see https://x.example/a/b, then") == "https://x.example/a/b"
    assert _clean_url("no url here") == ""
