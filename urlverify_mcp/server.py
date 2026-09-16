"""MCP server entry (stdio or Streamable HTTP)."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .models import VerifyRequest
from .pipeline import verify
from .promptstore import get_store
from .storage import Storage
from .tracelog import TRACE, configure_from

_cfg: Config | None = None
_store: Storage | None = None


def _init(config_path: str | None = None) -> tuple[Config, Storage]:
    global _cfg, _store
    if _cfg is None:
        _cfg = load_config(config_path)
        _store = Storage(_cfg.storage.resolved())
    return _cfg, _store  # type: ignore[return-value]


def build_server(config_path: str | None = None, host: str = "127.0.0.1", port: int = 8766) -> FastMCP:
    cfg, store = _init(config_path)
    configure_from(cfg)
    prompts = get_store(cfg.prompts.dir)
    # MCP-facing texts are read once here: editing them in the admin UI requires a server restart.
    mcp = FastMCP("URLVerify_MCP", host=host, port=port, instructions=prompts.get("mcp_instructions"))

    @mcp.tool(description=prompts.get("mcp_tool_verify_source"))
    async def verify_source(project: str, url: str, description: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        TRACE.log("mcp_request", tool="verify_source", args={"project": project, "url": url, "description": description, "options": options})
        res = await verify(VerifyRequest(project=project, url=url, description=description, options=options), cfg, store)
        out = res.model_dump(mode="json")
        TRACE.log("mcp_response", tool="verify_source", trace_id_result=res.trace_id, verdict=res.verdict.value, response=out)
        return out

    @mcp.tool(description=prompts.get("mcp_tool_get_verification"))
    async def get_verification(trace_id: str) -> dict[str, Any]:
        TRACE.log("mcp_request", tool="get_verification", args={"trace_id": trace_id})
        h = store.get_history(trace_id)
        out = h["result"] if h else {"error": "not found"}
        TRACE.log("mcp_response", tool="get_verification", response=out)
        return out

    @mcp.tool(description=prompts.get("mcp_tool_list_known_identities"))
    async def list_known_identities() -> list[dict[str, Any]]:
        TRACE.log("mcp_request", tool="list_known_identities", args={})
        out = [{"project": r["project"], **r["data"]} for r in store.dump_table("identity_cache")]
        TRACE.log("mcp_response", tool="list_known_identities", response=out)
        return out

    return mcp
