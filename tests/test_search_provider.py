import asyncio
import socket

import pytest

from urlverify_mcp.config import Config
from urlverify_mcp.providers.search import MCPSearchProvider, SearchUnavailable, SearxngHTTPProvider


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_mcp_provider_dead_endpoint_raises_ordinary_exception():
    cfg = Config()
    cfg.search.mcp.url = f"http://127.0.0.1:{_free_port()}/mcp"
    p = MCPSearchProvider(cfg)

    async def run():
        try:
            await p.search("x")
        except Exception as e:           # must be catchable by a plain `except Exception`
            return e
        finally:
            await p.close()
        return None

    err = asyncio.run(run())
    assert isinstance(err, SearchUnavailable), err
    assert "unreachable" in str(err)


def test_searxng_http_dead_endpoint_raises_ordinary_exception():
    cfg = Config()
    cfg.search.searxng_http.base_url = f"http://127.0.0.1:{_free_port()}"
    p = SearxngHTTPProvider(cfg)

    async def run():
        try:
            await p.search("x")
        except Exception as e:
            return e
        finally:
            await p.close()

    assert isinstance(asyncio.run(run()), SearchUnavailable)
