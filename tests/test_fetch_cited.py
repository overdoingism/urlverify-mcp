"""Pages the investigator quoted without fetching are fetched by the engine (bounded, third-party tier 1/2 only), so a
true quote is not lost for a mechanical reason; the quote must still match verbatim (AGENTS §6.3)."""
from urlverify_mcp.config import Config
from urlverify_mcp.evidence import EvidenceStore
from urlverify_mcp.models import Evidence, IdentityGraph, L0Result, LLMSubmission
from urlverify_mcp.pipeline import fetch_cited
from urlverify_mcp.rules import verify_quotes

CHOCO = "https://community.chocolatey.org/packages/vulkan-sdk"


class Fetcher:
    def __init__(self):
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        if "fail" in url:
            raise RuntimeError("boom")
        return "Vulkan SDK 1.4.363 Software Author(s): LunarG and Khronos Group url = https://sdk.lunarg.com/sdk/download/"


def _sub(*sources):
    ev = [Evidence(kind="media", source=s, tier=1, claim="c", quote="Software Author(s): LunarG and Khronos Group") for s in sources]
    return LLMSubmission(identity=IdentityGraph(product="Vulkan SDK", official_domains=["lunarg.com"]), evidence=ev)


async def test_engine_fetches_unfetched_third_party_citations():
    store, f, notes = EvidenceStore(), Fetcher(), []
    l0 = L0Result(normalized_url="https://sdk.lunarg.com/x.exe", host="sdk.lunarg.com", etld1="lunarg.com")
    sub = _sub(CHOCO, "https://www.lunarg.com/news/",                 # own domain: never fetched (self pages do not count)
               "https://reddit.com/r/vulkan/comments/1",             # tier 3: not fetched
               "https://fail.example.org/a", "https://b.techcrunch.com/x", "https://www.theverge.com/y", "https://arstechnica.com/z")
    sub.evidence[3].tier = 2
    got = await fetch_cited(sub, store, f, l0, Config(), notes)
    assert f.urls[0] == CHOCO and "https://www.lunarg.com/news/" not in f.urls and not any("reddit" in u for u in f.urls)
    assert len(f.urls) == 3 and got[0] == CHOCO                       # at most three fetches
    verify_quotes(sub.evidence[:1], store)
    assert sub.evidence[0].verified_quote                               # the real quote now verifies


async def test_wrong_quote_still_fails_and_fetched_pages_are_not_refetched():
    store, f = EvidenceStore(), Fetcher()
    l0 = L0Result(normalized_url="https://sdk.lunarg.com/x.exe", host="sdk.lunarg.com", etld1="lunarg.com")
    sub = _sub(CHOCO)
    sub.evidence[0].quote = "LunarG is the only official source of malware-free builds"
    await fetch_cited(sub, store, f, l0, Config(), [])
    verify_quotes(sub.evidence, store)
    assert not sub.evidence[0].verified_quote
    f2 = Fetcher()
    await fetch_cited(_sub(CHOCO), store, f2, l0, Config(), [])
    assert f2.urls == []
