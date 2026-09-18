"""Admin UI: FastAPI + single-page HTML. Config, allow/deny lists, caches, history, manual test."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .auth import COOKIE, AdminAuth
from pydantic import BaseModel

from ..config import Config, load_config, save_config
from ..health import HEALTH
from ..promptstore import PROMPTS, get_store
from ..tracelog import TRACE, configure_from
from ..models import VerifyRequest
from ..pipeline import verify
from ..storage import Storage

STATIC = Path(__file__).parent / "static"


class VerifyBody(BaseModel):
    project: str
    url: str
    description: str = ""
    options: dict[str, Any] | None = None


class FullLogBody(BaseModel):
    enabled: bool


class PromptBody(BaseModel):
    text: str


class LoginBody(BaseModel):
    password: str


class PasswordBody(BaseModel):
    current: str
    new: str


class State:
    def __init__(self, config_path: str | None):
        self.config_path = config_path
        self.cfg: Config = load_config(config_path)
        self.store = Storage(self.cfg.storage.resolved(), self.cfg.log.resolved())
        configure_from(self.cfg)
        self.prompts = get_store(self.cfg.prompts.dir)
        self.auth = AdminAuth(self.cfg.admin.auth_file, self.cfg.admin.session_days)
        HEALTH.attach(self.store)

    def reload(self):
        self.cfg = load_config(self.config_path)
        configure_from(self.cfg)
        self.prompts = get_store(self.cfg.prompts.dir)


def _os_open(path) -> dict:
    """Open a file or folder with the desktop's default handler. Local-only convenience; reports failure instead of raising."""
    import platform, subprocess
    try:
        sysname = platform.system()
        if sysname == "Windows":
            import os
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sysname == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "path": str(path), "dir": str(path)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": str(path), "dir": str(path), "error": f"{type(e).__name__}: {e}"}


def create_app(config_path: str | None = None) -> FastAPI:
    st = State(config_path)
    app = FastAPI(title="URLVerify_MCP admin")

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if path in ("/login", "/api/login") or st.auth.check_session(request.cookies.get(COOKIE)):
            resp = await call_next(request)
            resp.headers["Cache-Control"] = "no-store"     # the UI changes with every release; never serve a stale page
            return resp
        if path.startswith("/api/"):
            return JSONResponse({"error": "login required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return (STATIC / "login.html").read_text(encoding="utf-8")

    @app.post("/api/login")
    async def login(body: LoginBody, response: Response):
        if not st.auth.verify_password(body.password):
            raise HTTPException(401, "wrong password")
        response.set_cookie(COOKIE, st.auth.issue_session(), max_age=st.auth.session_seconds, httponly=True, samesite="lax")
        return {"ok": True, "default_password": st.auth.is_default()}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/auth")
    async def auth_status():
        return {"default_password": st.auth.is_default(), "auth_file": str(st.auth.path), "session_days": st.cfg.admin.session_days}

    @app.put("/api/password")
    async def change_password(body: PasswordBody, response: Response):
        if not st.auth.verify_password(body.current):
            raise HTTPException(401, "current password is wrong")
        try:
            st.auth.set_password(body.new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        response.set_cookie(COOKIE, st.auth.issue_session(), max_age=st.auth.session_seconds, httponly=True, samesite="lax")
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    async def get_config():
        d = st.cfg.model_dump(mode="json")
        d["_source_path"] = str(st.cfg.source_path) if st.cfg.source_path else None
        return d

    @app.put("/api/config")
    async def put_config(body: dict[str, Any]):
        body.pop("_source_path", None)
        try:
            cfg = Config.model_validate(body)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        cfg.source_path = st.cfg.source_path
        path = save_config(cfg)
        st.reload()
        return {"ok": True, "path": str(path)}

    @app.get("/api/cache/{table}")
    async def get_cache(table: str):
        if table not in ("cert_cache", "identity_cache", "anchor_cache"):
            raise HTTPException(404)
        rows = st.store.dump_table(table)
        if table == "anchor_cache":
            from ..cache.anchors import SEED
            rows = [{"etld1": d, "data": {"platform": a.platform, "seed": True, "expected_issuers": a.expected_issuers, "description": a.description}} for a in SEED for d in a.etld1s] + rows
        return rows

    @app.delete("/api/cache/{table}/{key:path}")
    async def del_cache(table: str, key: str):
        if table not in ("cert_cache", "identity_cache", "anchor_cache"):
            raise HTTPException(404)
        st.store.delete_row(table, key)
        return {"ok": True}

    @app.delete("/api/cache/{table}")
    async def clear_cache(table: str):
        st.store.clear_table(table)
        return {"ok": True}

    @app.get("/api/history")
    async def history(limit: int = 100):
        return st.store.list_history(limit)

    @app.delete("/api/history")
    async def clear_history():
        return {"ok": True, "removed": st.store.clear_history()}

    @app.get("/api/history/{trace_id}")
    async def history_item(trace_id: str):
        h = st.store.get_history(trace_id)
        if not h:
            raise HTTPException(404)
        return h

    @app.post("/api/verify")
    async def api_verify(body: VerifyBody):
        res = await verify(VerifyRequest(**body.model_dump()), st.cfg, st.store)
        return res.model_dump(mode="json")

    # ---- full data log
    @app.get("/api/fulllog")
    async def fulllog_status():
        return {"enabled": TRACE.enabled, "dir": str(TRACE.dir), "max_bytes": TRACE.max_bytes,
                "current": TRACE.current_path(), "files": TRACE.files()}

    @app.put("/api/fulllog")
    async def fulllog_toggle(body: FullLogBody):
        """Persist the toggle to config.yaml and apply it immediately (no restart)."""
        st.cfg.full_log.enabled = body.enabled
        save_config(st.cfg)
        st.reload()
        return {"ok": True, "enabled": TRACE.enabled, "path": str(st.cfg.source_path)}

    @app.post("/api/fulllog/open")
    async def fulllog_open():
        """Open the log folder in the OS file manager (admin UI is local-only by default)."""
        d = TRACE.dir
        d.mkdir(parents=True, exist_ok=True)
        return _os_open(d)

    @app.get("/api/tier1paths")
    async def tier1paths():
        from ..identity.sources import tier1_paths_status
        return tier1_paths_status()

    @app.post("/api/tier1paths/open")
    async def tier1paths_open():
        """Open data/tier1_paths.yaml in the local default editor."""
        from ..identity.sources import TIER1_PATHS_FILE
        return _os_open(TIER1_PATHS_FILE)

    @app.get("/api/fulllog/{name}")
    async def fulllog_read(name: str, tail: int = 262144):
        try:
            return {"name": name, "text": TRACE.read(name, tail)}
        except FileNotFoundError:
            raise HTTPException(404)

    # ---- prompts
    @app.get("/api/prompts")
    async def prompts_list():
        return st.prompts.listing()

    @app.get("/api/prompts/{name}")
    async def prompts_get(name: str):
        if name not in PROMPTS:
            raise HTTPException(404)
        return {"name": name, "meta": st.prompts.listing()[list(PROMPTS).index(name)],
                "default": st.prompts.default(name), "effective": st.prompts.get(name), "overridden": st.prompts.is_overridden(name)}

    @app.put("/api/prompts/{name}")
    async def prompts_put(name: str, body: PromptBody):
        if name not in PROMPTS:
            raise HTTPException(404)
        try:
            path = st.prompts.set(name, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "path": str(path), "live": PROMPTS[name].live}

    @app.delete("/api/prompts/{name}")
    async def prompts_reset(name: str):
        if name not in PROMPTS:
            raise HTTPException(404)
        st.prompts.reset(name)
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        from .. import __version__
        c = st.cfg
        return {"version": __version__, "config_path": str(c.source_path) if c.source_path else None,
                "state_dir": str(c.storage.resolved()), "log_dir": str(c.log.resolved()), "full_log": c.full_log.enabled,
                "search_provider": c.search.provider, "fetch_provider": c.fetch.provider, "llm_model": c.llm.model, "llm_base_url": c.llm.base_url}

    @app.get("/api/health")
    async def health_table():
        """Observed dependency health: what real calls reported. A report only; never a gate."""
        return {"rows": HEALTH.table(), "note": "observed from real calls; a report, never a gate: networks flap and the next call is always attempted"}

    @app.post("/api/checkenv")
    async def check_env():
        """Manual lightweight probes (same as `urlverify-mcp check-env`). Runs only when the button is pressed."""
        from ..diagnostics import probe_all
        return {"results": await probe_all(st.cfg)}

    return app
