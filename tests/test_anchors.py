from urlverify_mcp.cache.anchors import anchor_for, resolve


def _r(url):
    from urllib.parse import urlsplit
    from urlverify_mcp.checks.urltools import etld1_of
    u = urlsplit(url)
    a = anchor_for(etld1_of(u.hostname))
    return resolve(a, u.hostname, u.path)


def test_owner_in_path_host_or_nowhere():
    assert _r("https://github.com/overdoingism/urlverify-mcp/") == ("user_content", "overdoingism", "urlverify-mcp")
    assert _r("https://github.com/overdoingism/urlverify-mcp/releases/download/v0.1.2/x.zip")[:2] == ("user_content", "overdoingism")
    assert _r("https://raw.githubusercontent.com/overdoingism/urlverify-mcp/main/README.md")[:2] == ("user_content", "overdoingism")
    assert _r("https://overdoingism.github.io/")[:2] == ("user_content", "overdoingism")          # owner in host
    assert _r("https://evil.github.io/notepad/")[:2] == ("user_content", "evil")
    assert _r("https://release-assets.githubusercontent.com/github-production-release-asset/1/2") == ("user_content", None, None)
    assert _r("https://hub.docker.com/r/library/nginx") == ("user_content", "library", "nginx")
    assert _r("https://hub.docker.com/u/someone") == ("user_content", "someone", None)


def test_company_sites_are_not_user_content():
    assert _r("https://desktop.docker.com/win/main/amd64/1/Docker%20Desktop%20Installer.exe") == ("company_site", None, None)
    assert _r("https://www.docker.com/products/docker-desktop/") == ("company_site", None, None)
    assert _r("https://desktop.github.com/") == ("company_site", None, None)
    assert _r("https://docs.github.com/en/actions") == ("company_site", None, None)
    assert _r("https://github.com/features/copilot") == ("user_content", "features", "copilot")   # no reserved-name list: an "owner" that will simply never be established
    assert _r("https://github.com/") == ("company_site", None, None)
    assert _r("https://huggingface.co/docs/hub/index") == ("user_content", "docs", "hub")
    assert _r("https://huggingface.co/Qwen/Qwen3.8-Flash-Next") == ("user_content", "Qwen", "Qwen3.8-Flash-Next")
