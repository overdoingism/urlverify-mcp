"""Version resolution and WinGet manifest selection against canned registry responses (no network)."""
import json

import httpx
import pytest

from urlverify_mcp.source.parse import parse_source
from urlverify_mcp.source.resolve import Resolver
from urlverify_mcp.source.semver import max_satisfying

V = ["1.0.0", "1.2.0", "1.2.5", "1.3.0-beta.1", "1.3.0", "2.0.0-rc.1", "2.0.0", "2.1.3", "3.0.0-alpha"]


@pytest.mark.parametrize("rng,want", [("^1.2.0", "1.3.0"), ("~1.2.0", "1.2.5"), ("1.x", "1.3.0"), (">=1.2 <2", "1.3.0"),
                                      ("1.2.0 - 2.0.0", "2.0.0"), ("<2.0.0 || >=3", "1.3.0"), ("*", "2.1.3"),
                                      ("^2.0.0-rc.1", "2.1.3"), (">2.1", None), ("^0.1", None), ("=1.2.0", "1.2.0"),
                                      ("~1", "1.3.0"), ("<=1.2", "1.2.5"), (">1", "2.1.3")])
def test_npm_ranges(rng, want):
    assert max_satisfying(V, rng) == want


def test_npm_range_garbage_raises():
    with pytest.raises(ValueError):
        max_satisfying(V, "latest-ish!!")


def _transport(routes):
    def handler(req: httpx.Request):
        for pat, (code, body) in routes.items():
            if pat in str(req.url):
                return httpx.Response(code, text=body if isinstance(body, str) else json.dumps(body))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


async def _resolve(src, routes, hint=""):
    r = Resolver(5, "t", transport=_transport(routes))
    try:
        out = []
        for s in parse_source(src).subjects:
            out += await r.resolve(s, hint)
        return out
    finally:
        await r.close()


PYPI = {"info": {"version": "2.32.3"}, "releases": {"2.31.0": [{"yanked": False}], "2.32.0": [{"yanked": True}],
                                                    "2.32.3": [{"yanked": False}], "3.0.0b1": [{"yanked": False}]}}


async def test_pypi_resolution():
    [s] = await _resolve("pip install requests", {"pypi.org/pypi/requests/json": (200, PYPI)})
    assert (s.version, s.url) == ("2.32.3", "https://pypi.org/project/requests/2.32.3/")
    [s] = await _resolve("pip install 'requests<2.32.3'", {"pypi.org/pypi/requests/json": (200, PYPI)})
    assert s.version == "2.31.0"                                  # 2.32.0 is fully yanked
    [s] = await _resolve("pip install --pre 'requests>=3.0.0a0'", {"pypi.org/pypi/requests/json": (200, PYPI)})
    assert s.version == "3.0.0b1"
    [s] = await _resolve("pip install requests==9.9", {"pypi.org/pypi/requests/json": (200, PYPI)})
    assert s.codes == ["VERSION_NOT_FOUND"]
    [s] = await _resolve("pip install reqeusts", {})
    assert "PACKAGE_NOT_FOUND" in s.notes and not s.codes and s.url == "https://pypi.org/project/reqeusts/"


async def test_npm_and_nuget_resolution():
    npm = {"dist-tags": {"latest": "4.17.21", "next": "5.0.0-rc"}, "versions": {v: {} for v in ["4.17.20", "4.17.21", "5.0.0-rc"]}}
    [s] = await _resolve("npm i lodash@^4.0.0", {"registry.npmjs.org/lodash": (200, npm)})
    assert s.url == "https://www.npmjs.com/package/lodash/v/4.17.21"
    [s] = await _resolve("npm i lodash@next", {"registry.npmjs.org/lodash": (200, npm)})
    assert s.version == "5.0.0-rc"
    [s] = await _resolve("npm i @electron/asar", {"registry.npmjs.org/@electron%2Fasar": (200, npm)})
    assert s.url.startswith("https://www.npmjs.com/package/@electron/asar/v/")
    nuget = {"versions": ["12.0.3", "13.0.1", "13.0.3", "14.0.0-beta1"]}
    [s] = await _resolve("dotnet add package Newtonsoft.Json", {"newtonsoft.json/index.json": (200, nuget)})
    assert s.url == "https://www.nuget.org/packages/Newtonsoft.Json/13.0.3"
    [s] = await _resolve("dotnet add package Newtonsoft.Json -v '[12.0,13.0.2]'", {"newtonsoft.json/index.json": (200, nuget)})
    assert s.version == "13.0.1"


WINGET_LIST = [{"name": "4.90.0", "type": "dir"}, {"name": "4.91.0", "type": "dir"}, {"name": "Edge", "type": "dir"}]
WINGET_YAML = """PackageIdentifier: Docker.DockerDesktop
PackageVersion: 4.91.0
InstallerType: exe
Installers:
- Architecture: x64
  InstallerUrl: https://desktop.docker.com/win/main/amd64/239619/Docker%20Desktop%20Installer.exe
  InstallerSha256: AC405B09
- Architecture: arm64
  InstallerUrl: https://desktop.docker.com/win/main/arm64/239619/Docker%20Desktop%20Installer.exe
  InstallerSha256: BD11
ManifestType: installer
"""


WINGET_LOCALE = """PackageIdentifier: Docker.DockerDesktop
PackageVersion: 4.91.0
Publisher: Docker Inc.
PublisherUrl: https://www.docker.com/
PackageName: Docker Desktop
PackageUrl: https://www.docker.com/products/docker-desktop
License: Proprietary
"""


async def test_winget_manifest_selection():
    routes = {"contents/manifests/d/Docker/DockerDesktop": (200, WINGET_LIST),
              "4.91.0/Docker.DockerDesktop.installer.yaml": (200, WINGET_YAML),
              "4.91.0/Docker.DockerDesktop.locale.en-US.yaml": (200, WINGET_LOCALE),
              "4.91.0/Docker.DockerDesktop.yaml": (200, "PackageIdentifier: Docker.DockerDesktop\nDefaultLocale: en-US\nManifestType: version\n")}
    [s] = await _resolve("winget install --id Docker.DockerDesktop -e", routes, hint="Windows x64 installer")
    loc = s.seeds[1]
    assert loc.source.endswith("Docker.DockerDesktop.locale.en-US.yaml")
    assert "PublisherUrl: https://www.docker.com/" in loc.quote and "PackageName: Docker Desktop" in loc.quote
    assert s.version == "4.91.0" and "/amd64/" in s.url
    seed = s.seeds[0]
    assert seed.kind == "distro" and "microsoft/winget-pkgs/master/" in seed.source
    assert seed.quote.startswith("PackageIdentifier: Docker.DockerDesktop ... InstallerUrl: https://desktop.docker.com/")
    from urlverify_mcp.models import Evidence
    from urlverify_mcp.rules import verify_quotes
    ev = Evidence(kind="distro", source=seed.source, claim=seed.claim, quote=seed.quote)
    verify_quotes([ev], {seed.source: seed.text})
    assert ev.verified_quote
    both = await _resolve("winget install --id Docker.DockerDesktop -e", routes)
    assert len(both) == 2 and all("WINGET_ONE_OF_2_INSTALLERS" in x.notes for x in both)
    [s] = await _resolve("winget install --id Docker.DockerDesktop -e -a arm64", routes)
    assert "/arm64/" in s.url
    [s] = await _resolve("winget install Nope.Missing", {})
    assert s.codes == ["WINGET_QUERY_NOT_EXACT"]
    [s] = await _resolve("winget install --id Docker.DockerDesktop -v 1.0", routes)
    assert s.codes == ["VERSION_NOT_FOUND"]


async def test_winget_rate_limit_page_is_a_lookup_failure():
    routes = {"contents/manifests/d/Docker/DockerDesktop": (200, WINGET_LIST),
              "4.91.0/Docker.DockerDesktop.installer.yaml": (200, "Rate limited.\nFor more on scraping GitHub see https://x: terms")}
    [s] = await _resolve("winget install --id Docker.DockerDesktop -e", routes)
    assert s.codes == ["RESOLUTION_FAILED:winget"]


CASK = ('{"token":"docker-desktop","name":["Docker Desktop"],"homepage":"https://www.docker.com/products/docker-desktop",'
        '"url":"https://desktop.docker.com/mac/main/arm64/1/Docker.dmg","version":"4.92.0,1",'
        '"variations":{"sequoia":{"url":"https://desktop.docker.com/mac/main/amd64/1/Docker.dmg"},"x86_64_linux":{"url":"https://x/linux"}}}')
FORMULA = '{"name":"wget","homepage":"https://www.gnu.org/software/wget/","urls":{"stable":{"url":"https://ftpmirror.gnu.org/wget/wget-1.25.0.tar.gz"}},"versions":{"stable":"1.25.0"}}'
SCOOP = """{
    "version": "26.03",
    "homepage": "https://www.7-zip.org",
    "architecture": {
        "64bit": {"url": "https://www.7-zip.org/a/7z2603-x64.msi"},
        "arm64": {"url": "https://www.7-zip.org/a/7z2603-arm64.exe#/dl.7z"}
    }
}"""


async def test_homebrew_and_scoop_resolution():
    from urlverify_mcp.models import Evidence
    from urlverify_mcp.rules import verify_quotes
    both = await _resolve("brew install --cask docker-desktop", {"api/cask/docker-desktop.json": (200, CASK)})
    assert [x.url.split("/")[5] for x in both] == ["arm64", "amd64"]                 # linux variation ignored
    [x] = await _resolve("brew install --cask docker-desktop", {"api/cask/docker-desktop.json": (200, CASK)}, hint="Intel x86_64 Mac")
    assert "/amd64/" in x.url and '"homepage":"https://www.docker.com/products/docker-desktop"' in x.seeds[0].quote
    [f] = await _resolve("brew install wget", {"api/formula/wget.json": (200, FORMULA)})
    assert f.url.endswith("wget-1.25.0.tar.gz") and f.version == "1.25.0" and "HOMEBREW_BUILDS_FROM_SOURCE" in f.notes
    [c] = await _resolve("brew install nosuch", {})
    assert c.codes == ["HOMEBREW_NOT_FOUND"]
    [sc] = await _resolve("scoop install 7zip", {"ScoopInstaller/Main/master/bucket/7zip.json": (200, SCOOP)}, hint="x64")
    assert sc.url == "https://www.7-zip.org/a/7z2603-x64.msi" and sc.version == "26.03"
    ev = Evidence(kind="distro", source=sc.seeds[0].source, claim="c", quote=sc.seeds[0].quote)
    verify_quotes([ev], {sc.seeds[0].source: sc.seeds[0].text})
    assert ev.verified_quote
    [amb] = await _resolve("scoop install firefox", {})
    assert amb.codes == ["SCOOP_BUCKET_AMBIGUOUS"]


async def test_go_vanity_import():
    page = '<html><head><meta name="go-import" content="go.uber.org/zap git https://github.com/uber-go/zap"></head></html>'
    [g] = await _resolve("go install go.uber.org/zap@v1.27.0", {"go.uber.org/zap?go-get=1": (200, page)})
    assert g.url == "https://go.uber.org/zap" and "GO_IMPORT:go.uber.org/zap->https://github.com/uber-go/zap" in g.notes
    [n] = await _resolve("go install example.invalid/x@v1", {})
    assert n.codes == ["GO_IMPORT_NOT_FOUND"]
