"""HTTP for untrusted URLs: resolve, reject non-public answers, then connect to that IP.

The original Host and TLS SNI are retained. Pools are isolated by hostname AND IP,
so different names sharing an IP cannot reuse each other's authenticated connection.
Configured LLM/search service endpoints deliberately do not use this transport.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx


class UnsafeURL(httpx.RequestError):
    pass


async def public_addresses(host: str, port: int) -> list[str]:
    from ..checks.private import is_public_ip, looks_local_hostname
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if looks_local_hostname(host):
            raise UnsafeURL(f"non-public hostname: {host}")
        try:
            rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpx.ConnectError(f"DNS lookup failed for {host}: {exc}") from exc
        addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise httpx.ConnectError(f"no addresses for {host}")
    if any(not is_public_ip(ip) for ip in addresses):
        raise UnsafeURL(f"non-public address for {host}")
    return addresses


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.pools: dict[tuple, httpx.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme not in ("http", "https") or not url.host or url.userinfo:
            raise UnsafeURL("URL must be HTTP(S) without credentials", request=request)
        timeout = request.extensions.get("timeout", {}).get("connect", 10)
        try:
            async with asyncio.timeout(timeout):
                addresses = await public_addresses(url.host, url.port or (443 if url.scheme == "https" else 80))
        except TimeoutError as exc:
            raise httpx.ConnectTimeout("DNS lookup timed out", request=request) from exc
        last_error = None
        for ip in addresses:
            key = (url.scheme, url.host, url.port, ip)
            transport = self.pools.get(key)
            if transport is None:
                transport = self.pools[key] = httpx.AsyncHTTPTransport(trust_env=False)
            headers = request.headers.copy()
            headers["Host"] = url.netloc.decode("ascii")
            pinned = httpx.Request(request.method, url.copy_with(host=ip), headers=headers,
                                   stream=request.stream,
                                   extensions={**request.extensions, "sni_hostname": url.host})
            try:
                return await transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        raise last_error

    async def aclose(self) -> None:
        for transport in self.pools.values():
            await transport.aclose()


def public_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PublicTransport(), trust_env=False, **kwargs)
