import asyncio

from urlverify_mcp.config import Config
from urlverify_mcp.providers.fetch import html_to_text, make_fetcher, BuiltinFetcher
from urlverify_mcp.providers.search import NoSearchProvider, SearchUnavailable, make_search_provider


HTML = """<html><head><title>LM Studio &amp; friends</title><style>body{}</style><script>var x=1;</script></head>
<body><h1>Download</h1><p>Get it from <a href="/download">the download page</a> or see
<a href="https://github.com/lmstudio-ai/lms">GitHub</a>.</p><ul><li>Mac</li><li>Linux</li></ul>
<table><tr><td>a</td><td>b</td></tr></table><a href="#top">top</a><!-- hidden --></body></html>"""


def test_html_to_text_keeps_structure_and_links():
    t = html_to_text(HTML, "https://lmstudio.ai/")
    assert t.startswith("title: LM Studio & friends")
    assert "# Download" in t
    assert "the download page (https://lmstudio.ai/download)" in t          # relative link resolved
    assert "GitHub (https://github.com/lmstudio-ai/lms)" in t
    assert "- Mac" in t and "- Linux" in t
    assert "| a | b" in t
    assert "var x" not in t and "hidden" not in t and "body{}" not in t
    assert "top" in t and "(#top)" not in t                                 # anchors keep text, drop the target


def test_none_search_provider_and_factories():
    cfg = Config()
    cfg.search.provider = "none"
    p = make_search_provider(cfg)
    assert isinstance(p, NoSearchProvider)
    try:
        asyncio.run(p.search("x"))
        assert False, "should raise"
    except SearchUnavailable:
        pass
    f = make_fetcher(cfg, p)
    assert isinstance(f, BuiltinFetcher)
    asyncio.run(f.close())
    assert Config().search.provider == "searxng_http" and Config().fetch.provider == "builtin"
