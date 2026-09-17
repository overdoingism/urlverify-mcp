import asyncio

from urlverify_mcp.health import Health, begin_collect, end_collect, HEALTH, observe
from urlverify_mcp.storage import Storage


def test_observe_counts_escalates_and_recovers(tmp_path, capsys):
    h = Health()
    h.attach(Storage(tmp_path / "h.sqlite3"))
    h.observe("wayback", False, "HTTP 503")
    h.observe("wayback", False, "HTTP 503")
    h.observe("wayback", False, "HTTP 503")
    row = h.table()[0]
    assert row["dep"] == "wayback" and row["consecutive_fail"] == 3 and row["fail_count"] == 3
    err = capsys.readouterr().err
    assert "!! DEPENDENCY wayback" in err and "!!! DEPENDENCY DOWN wayback" in err
    h.observe("wayback", True)
    row = h.table()[0]
    assert row["consecutive_fail"] == 0 and row["ok_count"] == 1 and row["last_ok"]
    assert "recovered" in capsys.readouterr().err
    # persisted: a fresh Health attached to the same store sees the counts
    h2 = Health()
    h2.attach(Storage(tmp_path / "h.sqlite3"))
    assert h2.table()[0]["fail_count"] == 3


def test_degraded_collection_is_per_context():
    tok = begin_collect()
    observe("github", False, "boom")
    observe("pypi", True)
    assert end_collect(tok) == ["github"]
    tok = begin_collect()
    assert end_collect(tok) == []


def test_admin_health_and_checkenv(tmp_path, monkeypatch):
    from tests.test_admin_api import _client
    c, _ = _client(tmp_path, monkeypatch)
    r = c.get("/api/health").json()
    assert "rows" in r and "never" in r["note"]
    import urlverify_mcp.diagnostics as diag

    async def fake(cfg):
        return [{"name": "llm", "target": "x", "ok": True, "detail": "d", "ms": 1}]
    monkeypatch.setattr(diag, "probe_all", fake)
    r = c.post("/api/checkenv").json()
    assert r["results"][0]["name"] == "llm"
    assert c.get("/api/env").status_code == 404      # the automatic probe endpoint is gone
