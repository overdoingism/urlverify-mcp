"""Live regression over tests/fixtures/cases.yaml. Skipped unless --live is given (needs LLM + SearXNG MCP + network)."""
import asyncio
import pytest
import yaml
from pathlib import Path



CASES = yaml.safe_load((Path(__file__).parent / "fixtures" / "cases.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_live_case(case, request):
    if not request.config.getoption("--live", default=False):
        pytest.skip("live test; run with --live")
    from urlverify_mcp.config import load_config
    from urlverify_mcp.source.run import SourceRequest, verify_source
    from urlverify_mcp.storage import Storage
    cfg = load_config()
    store = Storage(cfg.storage.resolved(), cfg.log.resolved())
    req = SourceRequest(project=case["project"], source=case.get("source") or case["url"], description=case["description"],
                        artifact=case.get("artifact", ""), version=case.get("version", ""))
    res = asyncio.run(verify_source(req, cfg, store))
    print(res.model_dump_json(indent=1))
    if "expect_any" in case:
        assert res.verdict.value in case["expect_any"], res.codes
    else:
        assert res.verdict.value == case["expect"], res.codes
    first = res.subjects[0].result if res.subjects and res.subjects[0].result else None
    if "expect_path" in case:
        assert first and first.path == case["expect_path"], (first and first.path, res.codes)
    if "expect_risk_prefix" in case:
        assert first and any(r.startswith(case["expect_risk_prefix"]) for r in first.risk_signals), first and first.risk_signals
    if "expect_next_action" in case:
        assert res.next_action == case["expect_next_action"], res.next_action
