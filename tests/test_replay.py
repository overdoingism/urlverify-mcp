"""Offline replay of captured real verifications through the rules engine.

Fixtures live in tests/fixtures/replay/*.json.gz (see urlverify_mcp/devtools/capture.py). Each has an `expect` block
written by hand after review: {"verdict": ..., "min_confidence": x, "max_confidence": y, "why": "..."}. A fixture
without `expect` is skipped. Runs in milliseconds; no network, no LLM."""
from pathlib import Path

import pytest

from urlverify_mcp.config import Config
from urlverify_mcp.devtools.capture import load
from urlverify_mcp.evidence import EvidenceStore
from urlverify_mcp.models import L0Result, LLMSubmission, Verdict
from urlverify_mcp.rules import decide

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "replay").glob("*.json.gz"))


@pytest.mark.parametrize("path", FIXTURES, ids=[p.name.split(".")[0] for p in FIXTURES])
def test_replay(path):
    case = load(path)
    exp = case.get("expect")
    if not exp:
        pytest.skip("no expect block yet")
    cfg = Config(**case["config"])
    d = decide(cfg, L0Result(**case["l0"]), LLMSubmission(**case["submission"]), EvidenceStore.from_json(case["store"]), case["project"],
               case.get("cached_identity"), case.get("ages") or {}, case.get("target_domain_age"),
               case.get("provenance"), case.get("registry_state"))
    assert d.verdict == Verdict(exp["verdict"]), (exp.get("why"), d.notes)
    assert exp.get("min_confidence", 0) <= d.confidence <= exp.get("max_confidence", 1), (d.confidence, d.notes)


def _replay(name):
    path = next(p for p in FIXTURES if p.name.startswith(name))
    case = load(path)
    return decide(Config(**case["config"]), L0Result(**case["l0"]), LLMSubmission(**case["submission"]),
                  EvidenceStore.from_json(case["store"]), case["project"], case.get("cached_identity"),
                  case.get("ages") or {}, case.get("target_domain_age"), case.get("provenance"), case.get("registry_state"))


def test_edges_reported():
    d = _replay("llama-cpp-6ae")
    assert "PROJECT_TO_ORG:github:ggml-org" in d.established_edges and not d.missing_edges
    d = _replay("rocmfpx")
    assert [m["edge"] for m in d.missing_edges] == ["PROJECT_TO_ORG:github:charlie12345"]
    assert d.missing_edges[0]["need"] == 2 and d.missing_edges[0]["have"] <= 1
    d = _replay("vulkan-sdk")
    assert "PROJECT_TO_DOMAIN:lunarg.com" in d.established_edges and [m["edge"] for m in d.missing_edges] == ["PROJECT_NAME_MATCH"]
    d = _replay("electron-asar")
    assert {"PACKAGE_TO_REPOSITORY", "PROJECT_TO_ORG:npm:@electron/asar"} <= set(d.established_edges)
