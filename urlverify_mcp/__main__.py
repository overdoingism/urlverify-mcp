"""CLI: urlverify-mcp serve | admin | verify | init-config"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path


def _plain_logs() -> None:
    """Disable ANSI colours in uvicorn's log formatters. Windows consoles drop VT processing after a child
    process has run, which turns colour codes into literal '[32m' noise; plain text is also friendlier for files."""
    try:
        import uvicorn.config as uc
        for name in ("default", "access"):
            uc.LOGGING_CONFIG["formatters"][name]["use_colors"] = False
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    _plain_logs()
    ap = argparse.ArgumentParser(prog="urlverify-mcp", description="URLVerify_MCP — source-of-origin verification MCP server")
    from . import __version__
    ap.add_argument("--version", action="version", version=f"urlverify-mcp {__version__}")
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
        from .logsetup import setup as _log_setup
        from .server import build_server
        cfg = load_config(args.config)
        _log_setup(cfg, "server")
        transport = args.transport or cfg.server.transport
        host, port = args.host or cfg.server.host, args.port or cfg.server.port
        srv = build_server(args.config, host, port)
        if transport == "http":
            print(f"URLVerify_MCP Streamable HTTP endpoint: http://{host}:{port}/mcp", file=sys.stderr)
        try:
            if transport == "stdio":
                srv.run(transport="stdio")
            else:
                import uvicorn
                app = srv.streamable_http_app()
                if cfg.server.auth_token:
                    app = _bearer_guard(app, cfg.server.auth_token)
                    print("bearer token required on /mcp (server.auth_token)", file=sys.stderr)
                elif host not in ("127.0.0.1", "localhost", "::1"):
                    print(f"WARNING: /mcp is bound to {host} without server.auth_token; anyone who can reach it can run verifications", file=sys.stderr)
                uvicorn.run(app, host=host, port=port, log_level="info", use_colors=False)
        except KeyboardInterrupt:
            print("URLVerify_MCP server stopped (Ctrl+C)", file=sys.stderr)
            return 0
        return 0

    if args.cmd == "admin":
        import uvicorn
        from .admin.app import create_app
        from .config import load_config
        cfg = load_config(args.config)
        from .logsetup import setup as _log_setup
        _log_setup(cfg, "admin")
        app = create_app(args.config)
        try:
            uvicorn.run(app, host=args.host or cfg.admin.host, port=args.port or cfg.admin.port, log_level="info", use_colors=False)
        except KeyboardInterrupt:
            print("URLVerify_MCP admin stopped (Ctrl+C)", file=sys.stderr)
        return 0

    if args.cmd == "verify":
        from .config import load_config
        from .models import VerifyRequest
        from .pipeline import verify
        from .storage import Storage
        cfg = load_config(args.config)
        store = Storage(cfg.storage.resolved(), cfg.log.resolved())
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


def _bearer_guard(app, token: str):
    """ASGI wrapper: require `Authorization: Bearer <token>` on every HTTP request (constant-time compare)."""
    import hmac

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            auth = hdrs.get("authorization", "")
            ok = auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), token)
            if not ok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        await app(scope, receive, send)
    return guarded


async def _check_env(cfg) -> None:
    """Manual diagnostics of the configured endpoints (LLM, search, fetch) only. Third-party services are not
    probed; their state is observed from real verifications (admin Status tab / log/health.json)."""
    from .diagnostics import probe_all
    for r in await probe_all(cfg):
        print(f"{r['name']:10s} {('OK  ' if r['ok'] else 'FAIL'):4s} {r['ms']:>5d} ms  {r['target']}: {r['detail']}")


if __name__ == "__main__":
    sys.exit(main())
