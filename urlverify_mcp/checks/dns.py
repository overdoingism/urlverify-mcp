"""DNS resolution (stdlib only)."""
from __future__ import annotations

import asyncio
import socket
from typing import Any


def _resolve(host: str) -> dict[str, Any]:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        addrs = sorted({i[4][0] for i in infos})
        return {"resolved": True, "addresses": addrs}
    except socket.gaierror as e:
        return {"resolved": False, "error": str(e)}


async def resolve(host: str) -> dict[str, Any]:
    return await asyncio.to_thread(_resolve, host)
