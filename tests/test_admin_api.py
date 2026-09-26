import os

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    # as_posix(): YAML double-quoted scalars treat backslash as an escape char, so Windows paths must be slash-form
    tp = tmp_path.as_posix()
    cfg_path.write_text(
        f'storage:\n  dir: "{tp}/state"\nlog:\n  dir: "{tp}/log"\nfull_log:\n  enabled: false\n  dir: "{tp}/logs"\nprompts:\n  dir: "{tp}/prompts"\nadmin:\n  auth_file: "{tp}/admin.auth"\n',
        encoding="utf-8")
    import urlverify_mcp.promptstore as ps
    monkeypatch.setattr(ps, "_store", None)
    from urlverify_mcp.admin.app import create_app
    c = TestClient(create_app(str(cfg_path)))
    assert c.post("/api/login", json={"password": "admin"}).status_code == 200   # default password, sets the session cookie
    return c, cfg_path


def test_full_log_is_gone_and_old_config_still_loads(tmp_path, monkeypatch):
    """The full data log was removed (History is the record). A config.yaml that still has a full_log section loads."""
    c, _ = _client(tmp_path, monkeypatch)                       # the fixture config still carries full_log:
    assert c.get("/api/fulllog").status_code == 404
    assert "full_log" not in c.get("/api/config").json()
    assert "full_log" not in c.get("/api/status").json()


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


def test_login_required_and_password_change(tmp_path, monkeypatch):
    c, cfg_path = _client(tmp_path, monkeypatch)
    anon = TestClient(c.app)
    assert anon.get("/api/config").status_code == 401
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200
    assert anon.post("/api/login", json={"password": "nope"}).status_code == 401
    assert c.get("/api/auth").json()["default_password"] is True
    assert c.put("/api/password", json={"current": "wrong", "new": "s3cret"}).status_code == 401
    assert c.put("/api/password", json={"current": "admin", "new": "abc"}).status_code == 400
    assert c.put("/api/password", json={"current": "admin", "new": "s3cret"}).json()["ok"]
    assert c.get("/api/auth").json()["default_password"] is False
    fresh = TestClient(c.app)
    assert fresh.post("/api/login", json={"password": "admin"}).status_code == 401
    assert fresh.post("/api/login", json={"password": "s3cret"}).status_code == 200
    assert fresh.get("/api/config").status_code == 200
    # deleting the auth file resets to the default password and invalidates sessions
    import os
    os.remove(tmp_path / "admin.auth")
    from urlverify_mcp.admin.auth import AdminAuth
    a = AdminAuth(tmp_path / "admin.auth")
    assert a.verify_password("admin") and a.is_default()
    assert not a.check_session(fresh.cookies.get("urlverify_session"))


def test_admin_responses_are_not_cacheable(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.get("/").headers.get("cache-control") == "no-store"
    assert c.get("/api/config").headers.get("cache-control") == "no-store"


def test_cooldown_hours_config_roundtrip(tmp_path, monkeypatch):
    c, path = _client(tmp_path, monkeypatch)
    cfg = c.get('/api/config').json()
    assert cfg['release_cooldown']['hours'] == 72
    for hours in (12.5, 0):
        cfg['release_cooldown']['hours'] = hours
        assert c.put('/api/config', json=cfg).status_code == 200
        assert c.get('/api/config').json()['release_cooldown']['hours'] == hours
        from urlverify_mcp.config import load_config
        assert load_config(str(path)).release_cooldown.hours == hours
    cfg['release_cooldown']['hours'] = -1
    assert c.put('/api/config', json=cfg).status_code == 400


def test_github_token_masked_and_kept(tmp_path, monkeypatch):
    import yaml
    from fastapi.testclient import TestClient
    from urlverify_mcp.admin.app import create_app
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"storage": {"dir": str(tmp_path / "s")}, "log": {"dir": str(tmp_path / "l")},
                                        "admin": {"auth_file": str(tmp_path / "admin.auth")}}))
    app = create_app(str(cfg_path))
    c = TestClient(app)
    c.post("/api/login", json={"password": "admin"})
    assert c.put("/api/github_token", json={"token": "github_pat_abcdefgh1234"}).json()["set"]
    assert c.get("/api/github_token").json() == {"set": True, "hint": "…1234"}
    conf = c.get("/api/config").json()
    assert conf["identity"]["github_token"] == "********"
    assert c.put("/api/config", json=conf).json()["ok"]                       # saving the masked JSON keeps the token
    assert yaml.safe_load(cfg_path.read_text())["identity"]["github_token"] == "github_pat_abcdefgh1234"
    c.put("/api/github_token", json={"token": ""})
    assert c.get("/api/github_token").json()["set"] is False
