"""Refuse non-public targets. A verification server runs on the host with LAN access; letting an agent point it at
loopback / private / link-local addresses turns it into a reconnaissance tool and there is nothing to verify there."""
from __future__ import annotations

import ipaddress


def is_public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)



def non_public(addresses: list[str]) -> list[str]:
    return [a for a in addresses if not is_public_ip(a)]


LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet")


def looks_local_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    return h in ("localhost",) or h.endswith(LOCAL_HOST_SUFFIXES) or "." not in h
