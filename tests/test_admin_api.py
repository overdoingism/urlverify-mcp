import os

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    # as_posix(): YAML double-quoted scalars treat backslash as an escape char, so Windows paths must be slash-form
    tp = tmp_path.as_posix()
    cfg_path.write_text(
        f'storage:\n  path: "{tp}/t.sqlite3"\nfull_log:\n  enabled: false\n  dir: "{tp}/logs"\nprompts:\n  dir: "{tp}/prompts"\n',
        encoding="utf-8")
    import urlverify_mcp.promptstore as ps
    monkeypatch.setattr(ps, "_store", None)
    from urlverify_mcp.admin.app import create_app
    return TestClient(create_app(str(cfg_path))), cfg_path


def test_fulllog_toggle_persists_and_applies(tmp_path, monkeypatch):
    c, cfg_path = _client(tmp_path, monkeypatch)
    assert c.get("/api/fulllog").json()["enabled"] is False
    r = c.put("/api/fulllog", json={"enabled": True}).json()
    assert r["ok"] and r["enabled"] is True
    assert "enabled: true" in cfg_path.read_text()
    from urlverify_mcp.tracelog import TRACE
    TRACE.log("probe", x=1)
    files = c.get("/api/fulllog").json()["files"]
    assert files and files[0]["name"].startswith("full-")
    body = c.get(f"/api/fulllog/{files[0]['name']}").json()["text"]
    assert '"kind": "probe"' in body
    assert c.get("/api/fulllog/../../etc/passwd").status_code in (404, 422)
    c.put("/api/fulllog", json={"enabled": False})
    assert TRACE.enabled is False


def test_prompt_endpoints(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    names = [p["name"] for p in c.get("/api/prompts").json()]
    assert "agent_system" in names and "mcp_tool_verify_source" in names
    p = c.get("/api/prompts/agent_reason").json()
    assert p["effective"] == p["default"] and not p["overridden"]
    assert c.put("/api/prompts/agent_reason", json={"text": "broken"}).status_code == 400
    ok = c.put("/api/prompts/agent_system", json={"text": "custom system prompt\n"}).json()
    assert ok["ok"] and ok["live"] is True and os.path.isfile(ok["path"])
    assert c.get("/api/prompts/agent_system").json()["overridden"] is True
    assert c.delete("/api/prompts/agent_system").json()["ok"]
    assert c.get("/api/prompts/agent_system").json()["overridden"] is False
    assert c.get("/api/prompts/nope").status_code == 404
