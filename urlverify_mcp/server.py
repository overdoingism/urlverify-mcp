"""MCP server entry (stdio or Streamable HTTP)."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config, load_config
from .models import VerifyRequest
from .pipeline import verify
from .storage import Storage

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
    mcp = FastMCP("URLVerify_MCP", host=host, port=port,
                  instructions="Verify whether a download / install / data-source URL comes from an official channel. "
                               "Call verify_source with project, url and description. Verdicts: VERIFIED_TRUE, VERIFIED_FALSE, UNVERIFIABLE.")

    @mcp.tool()
    async def verify_source(project: str, url: str, description: str = "", options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Verify that `url` is an official / legitimate source for `project`.

        Args:
            project: project / product name, e.g. "LM Studio".
            url: the download, installer, repository, model or data-source URL to check.
            description: what the URL is supposed to be, e.g. "Linux x64 AppImage installer".
            options: optional overrides: {"min_sources": 2, "allow_tier3": false, "history_days": 90}.
        Returns a dict with verdict (VERIFIED_TRUE | VERIFIED_FALSE | UNVERIFIABLE), confidence, reason (in the caller's language),
        evidence[], checks{}, identity{}, risk_signals[], cache_hits[], engine_notes[], trace_id.
        """
        res = await verify(VerifyRequest(project=project, url=url, description=description, options=options), cfg, store)
        return res.model_dump(mode="json")

    @mcp.tool()
    async def get_verification(trace_id: str) -> dict[str, Any]:
        """Fetch a previous verification result by trace_id."""
        h = store.get_history(trace_id)
        return h["result"] if h else {"error": "not found"}

    @mcp.tool()
    async def list_known_identities() -> list[dict[str, Any]]:
        """List cached, independently-established project identities (official domains / orgs)."""
        return [{"project": r["project"], **r["data"]} for r in store.dump_table("identity_cache")]

    return mcp
