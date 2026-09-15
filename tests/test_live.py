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
    from urlverify_mcp.models import VerifyRequest
    from urlverify_mcp.pipeline import verify
    from urlverify_mcp.storage import Storage
    cfg = load_config()
    store = Storage(cfg.storage.resolved())
    res = asyncio.run(verify(VerifyRequest(project=case["project"], url=case["url"], description=case["description"]), cfg, store))
    print(res.model_dump_json(indent=1))
    assert res.verdict.value == case["expect"], res.engine_notes
