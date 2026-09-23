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
