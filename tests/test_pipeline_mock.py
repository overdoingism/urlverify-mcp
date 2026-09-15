"""Pipeline plumbing test: real L0 + real structured APIs + real SearXNG MCP, but a scripted LLM.
Needs network; skipped automatically when lmstudio.ai is unreachable."""
import asyncio
import json
import socket
import types

import pytest

from urlverify_mcp.config import load_config
from urlverify_mcp.models import VerifyRequest
from urlverify_mcp.storage import Storage


def _online() -> bool:
    try:
        socket.create_connection(("lmstudio.ai", 443), timeout=5).close()
        return True
    except OSError:
        return False


class ScriptedLLM:
    """Emulates native tool calling. Reads tool outputs to build verbatim quotes."""
    supports_tools = True

    def __init__(self, cfg):
        self.step = 0
        self.outputs = {}

    async def chat(self, messages, tools=None, json_mode=False, temperature=None):
        # collect the latest tool outputs
        for m in messages:
            if m.get("role") == "tool":
                self.outputs[m["tool_call_id"]] = m["content"]
        if tools is None:  # reason-writing call
            return types.SimpleNamespace(content="Scripted reason.", tool_calls=None)
        self.step += 1
        plan = {1: ("wikipedia_history", {"title": "LM Studio"}),
                2: ("github_info", {"owner": "lmstudio-ai", "repo": "lms"}),
                3: ("wayback_first_seen", {"domain": "lmstudio.ai"})}
        if self.step in plan:
            name, args = plan[self.step]
            return types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(
                id=f"c{self.step}", function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))])
        wiki, gh, wb = self.outputs.get("c1", ""), self.outputs.get("c2", ""), self.outputs.get("c3", "")
        sub = {
            "identity": {"product": "LM Studio", "developer": "Element Labs", "aliases": ["Bionic"],
                         "official_domains": ["lmstudio.ai"], "official_orgs": {"github": ["lmstudio-ai"]},
                         "narrative": "scripted"},
            "evidence": [
                {"kind": "wikipedia", "source": "https://en.wikipedia.org/wiki/LM_Studio", "tier": 1,
                 "claim": "official domain is lmstudio.ai", "quote": '"official_website": [\n  "lmstudio.ai"', "supports": True},
                {"kind": "github", "source": "https://github.com/lmstudio-ai/lms", "tier": 2,
                 "claim": "github org lmstudio-ai is verified and links to lmstudio.ai",
                 "quote": '"blog": "https://lmstudio.ai"', "supports": True},
                {"kind": "wayback", "source": "https://web.archive.org/web/*/lmstudio.ai", "tier": 1,
                 "claim": "lmstudio.ai has archive history since 2023", "quote": '"first_snapshot": "2023-05-24"', "supports": True},
                {"kind": "media", "source": "https://example.com/fake", "tier": 2,
                 "claim": "official domain is lmstudio.ai", "quote": "this quote never appeared anywhere", "supports": True},
            ],
            "proposed_verdict": "VERIFIED_TRUE", "proposed_reason": "scripted", "risk_notes": []}
        return types.SimpleNamespace(content="", tool_calls=[types.SimpleNamespace(
            id="c9", function=types.SimpleNamespace(name="submit_verdict", arguments=json.dumps(sub)))])


@pytest.mark.skipif(not _online(), reason="offline")
def test_pipeline_with_scripted_llm(monkeypatch, tmp_path):
    from urlverify_mcp import pipeline
    monkeypatch.setattr(pipeline, "LLM", ScriptedLLM)
    cfg = load_config()
    cfg.storage.path = str(tmp_path / "t.sqlite3")
    store = Storage(cfg.storage.resolved())
    from urlverify_mcp.pipeline import verify
    res = asyncio.run(verify(VerifyRequest(project="LM Studio", url="https://lmstudio.ai/download", description="download page"), cfg, store))
    print(res.model_dump_json(indent=1))
    assert res.checks["tls"]["status"] == "pass"
    assert res.checks["injection"]["status"] == "pass"
    fake = [e for e in res.evidence if e.source.endswith("/fake")][0]
    assert fake.verified_quote is False                      # hallucinated quote discarded
    assert res.verdict.value == "VERIFIED_TRUE", res.engine_notes
    assert "identity" not in res.cache_hits
    assert store.get_identity("LM Studio")["official_domains"] == ["lmstudio.ai"]

    # second run on a github URL uses the identity cache and checks the path owner
    res2 = asyncio.run(verify(VerifyRequest(project="LM Studio", url="https://github.com/lmstudio-ai/lms", description="cli"), cfg, store))
    assert "identity" in res2.cache_hits and "platform_anchor" in res2.cache_hits
    assert res2.verdict.value == "VERIFIED_TRUE", res2.engine_notes

    # look-alike org must fail even though the LLM script still says TRUE
    res3 = asyncio.run(verify(VerifyRequest(project="LM Studio", url="https://github.com/lm-studio-ai/lms", description="cli"), cfg, store))
    assert res3.verdict.value in ("VERIFIED_FALSE", "UNVERIFIABLE"), res3.engine_notes
