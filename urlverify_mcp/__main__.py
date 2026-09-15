"""CLI: urlverify-mcp serve | admin | verify | init-config"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="urlverify-mcp", description="URLVerify_MCP — source-of-origin verification MCP server")
    ap.add_argument("-c", "--config", help="path to config.yaml (default: ./config.yaml, $URLVERIFY_CONFIG, ~/.urlverify_mcp/config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "http"], help="override config server.transport (default stdio)")
    s.add_argument("--host", help="override config server.host")
    s.add_argument("--port", type=int, help="override config server.port (default 8766)")

    a = sub.add_parser("admin", help="run the admin web UI")
    a.add_argument("--host")
    a.add_argument("--port", type=int)

    v = sub.add_parser("verify", help="run one verification from the command line")
    v.add_argument("project"); v.add_argument("url"); v.add_argument("description", nargs="?", default="")
    v.add_argument("--min-sources", type=int); v.add_argument("--allow-tier3", action="store_true")

    sub.add_parser("init-config", help="write config.yaml from the example into the current directory")
    sub.add_parser("check-env", help="probe LLM and search endpoints")

    args = ap.parse_args(argv)

    if args.cmd == "init-config":
        src = Path(__file__).resolve().parent.parent / "config.example.yaml"
        dst = Path.cwd() / "config.yaml"
        if dst.exists():
            print(f"{dst} already exists"); return 1
        shutil.copy(src, dst); print(f"wrote {dst}"); return 0

    if args.cmd == "serve":
        from .config import load_config
        from .server import build_server
        cfg = load_config(args.config)
        transport = args.transport or cfg.server.transport
        host, port = args.host or cfg.server.host, args.port or cfg.server.port
        srv = build_server(args.config, host, port)
        if transport == "http":
            print(f"URLVerify_MCP Streamable HTTP endpoint: http://{host}:{port}/mcp", file=sys.stderr)
        srv.run(transport="stdio" if transport == "stdio" else "streamable-http")
        return 0

    if args.cmd == "admin":
        import uvicorn
        from .admin.app import create_app
        from .config import load_config
        cfg = load_config(args.config)
        app = create_app(args.config)
        uvicorn.run(app, host=args.host or cfg.admin.host, port=args.port or cfg.admin.port, log_level="info")
        return 0

    if args.cmd == "verify":
        from .config import load_config
        from .models import VerifyRequest
        from .pipeline import verify
        from .storage import Storage
        cfg = load_config(args.config)
        store = Storage(cfg.storage.resolved())
        opts = {}
        if args.min_sources: opts["min_sources"] = args.min_sources
        if args.allow_tier3: opts["allow_tier3"] = True
        res = asyncio.run(verify(VerifyRequest(project=args.project, url=args.url, description=args.description, options=opts or None), cfg, store))
        print(json.dumps(res.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if res.verdict.value == "VERIFIED_TRUE" else 2

    if args.cmd == "check-env":
        from .config import load_config
        cfg = load_config(args.config)
        print(f"config: {cfg.source_path or '(defaults)'}")
        asyncio.run(_check_env(cfg))
        return 0
    return 0


async def _check_env(cfg) -> None:
    import httpx
    print(f"LLM  {cfg.llm.base_url} model={cfg.llm.model}: ", end="")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(cfg.llm.base_url.rstrip('/') + "/models", headers={"Authorization": f"Bearer {cfg.llm.api_key}"})
            ids = [m.get("id") for m in r.json().get("data", [])][:5]
            print(f"OK {ids}")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL ({type(e).__name__}: {e})")
    print(f"search provider={cfg.search.provider}: ", end="")
    try:
        from .providers.search import make_search_provider
        p = make_search_provider(cfg)
        out = await p.search("URLVerify smoke test")
        await p.close()
        print(f"OK ({len(out)} chars)")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL ({type(e).__name__}: {e})")


if __name__ == "__main__":
    sys.exit(main())
