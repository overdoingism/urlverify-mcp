import asyncio
import time

from urlverify_mcp import progress
from urlverify_mcp.config import Config
from urlverify_mcp.models import VerifyRequest, Verdict


def test_report_and_events():
    sent = []

    async def rep(p, t, m):
        sent.append((p, t, m))

    async def run():
        pr = progress.Progress(rep, events=True, heartbeat_s=0)
        tok = progress.bind(pr)
        await progress.report("L0", 0.1)
        await progress.report("L1 turn 1")          # no fraction: keeps 10%
        await progress.report("back", 0.05)         # fraction never goes backwards
        progress.unbind(tok)
        await progress.report("ignored: nothing bound")
        return pr
    pr = asyncio.run(run())
    assert [m for _, _, m in sent] == ["L0", "L1 turn 1", "back"]
    assert sent[-1][0] == 10.0 and pr.stage().startswith("back at 10%")


def test_events_off_still_tracks_stage():
    sent = []

    async def rep(p, t, m):
        sent.append(m)

    async def run():
        pr = progress.Progress(rep, events=False, heartbeat_s=0)
        await pr.report("silent", 0.5)
        return pr
    pr = asyncio.run(run())
    assert sent == [] and pr.stage() == "silent at 50%"


def test_heartbeat_ticks_and_stops_with_task():
    sent = []

    async def rep(p, t, m):
        sent.append(m)

    async def run():
        pr = progress.Progress(rep, events=True, heartbeat_s=1)
        pr.start_heartbeat()
        await pr.report("working", 0.3)
        await asyncio.sleep(2.6)
        await pr.stop()
        n = len(sent)
        await asyncio.sleep(1.5)
        return n, len(sent)
    n_at_stop, n_after = asyncio.run(run())
    assert n_at_stop >= 3                      # 1 event + >=2 heartbeats
    assert n_after == n_at_stop                # nothing after stop
    assert any("still working" in m for m in sent)


def test_reporter_failure_does_not_break():
    async def bad(p, t, m):
        raise RuntimeError("client gone")

    async def run():
        pr = progress.Progress(bad, events=True, heartbeat_s=0)
        await pr.report("x", 0.2)
        return pr.sent
    assert asyncio.run(run()) == 0


def test_total_deadline_yields_unverifiable(monkeypatch, tmp_path):
    from urlverify_mcp import pipeline
    from urlverify_mcp.storage import Storage

    async def slow(req, cfg, store, trace_id, t0):
        await progress.report("L1: LLM turn 3/24", 0.4)
        await asyncio.sleep(10)
    monkeypatch.setattr(pipeline, "_verify", slow)
    cfg = Config()
    cfg.budget.max_total_s = 1
    cfg.storage.path = str(tmp_path / "t.sqlite3")
    store = Storage(cfg.storage.resolved())

    async def run():
        pr = progress.Progress(None, events=False, heartbeat_s=0)
        tok = progress.bind(pr)
        try:
            return await pipeline.verify(VerifyRequest(project="X", url="https://example.com/", description=""), cfg, store)
        finally:
            progress.unbind(tok)
    t = time.time()
    res = asyncio.run(run())
    assert time.time() - t < 5
    assert res.verdict == Verdict.UNVERIFIABLE and res.confidence == 0.0
    assert "time budget of 1s" in res.reason and "LLM turn 3/24" in res.reason
    assert store.get_history(res.trace_id)["verdict"] == "UNVERIFIABLE"
