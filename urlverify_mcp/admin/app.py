"""Admin UI: FastAPI + single-page HTML. Config, allow/deny lists, caches, history, manual test."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import Config, load_config, save_config
from ..models import VerifyRequest
from ..pipeline import verify
from ..storage import Storage

STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self, config_path: str | None):
        self.config_path = config_path
        self.cfg: Config = load_config(config_path)
        self.store = Storage(self.cfg.storage.resolved())

    def reload(self):
        self.cfg = load_config(self.config_path)


def create_app(config_path: str | None = None) -> FastAPI:
    st = State(config_path)
    app = FastAPI(title="URLVerify_MCP admin")

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

    @app.get("/api/history/{trace_id}")
    async def history_item(trace_id: str):
        h = st.store.get_history(trace_id)
        if not h:
            raise HTTPException(404)
        return h

    class VerifyBody(BaseModel):
        project: str
        url: str
        description: str = ""
        options: dict[str, Any] | None = None

    @app.post("/api/verify")
    async def api_verify(body: VerifyBody):
        res = await verify(VerifyRequest(**body.model_dump()), st.cfg, st.store)
        return res.model_dump(mode="json")

    @app.get("/api/env")
    async def env():
        import httpx
        out: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(st.cfg.llm.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {st.cfg.llm.api_key}"})
                out["llm"] = {"ok": r.status_code == 200, "models": [m.get("id") for m in r.json().get("data", [])][:10]}
        except Exception as e:  # noqa: BLE001
            out["llm"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            from ..providers.search import make_search_provider
            p = make_search_provider(st.cfg)
            s = await p.search("URLVerify smoke test")
            await p.close()
            out["search"] = {"ok": True, "chars": len(s)}
        except Exception as e:  # noqa: BLE001
            out["search"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return out

    return app
