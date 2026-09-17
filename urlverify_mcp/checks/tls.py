"""TLS certificate check: trusted chain (system store, certifi fallback), validity, SAN, Organization."""
from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import certifi


def _ctx(use_certifi: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where() if use_certifi else None)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _parse_ts(s: str) -> float:
    return datetime.strptime(s, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc).timestamp()


def _flatten(rdns) -> dict[str, str]:
    out: dict[str, str] = {}
    for rdn in rdns or ():
        for k, v in rdn:
            out.setdefault(k, v)
    return out


SCT_OID_DER = bytes.fromhex("060a2b06010401d679020402")   # OID 1.3.6.1.4.1.11129.2.4.2 (embedded SCT list) as DER


def has_embedded_scts(der: bytes) -> bool:
    """Public CAs embed Signed Certificate Timestamps; a locally issued interception certificate has none."""
    return SCT_OID_DER in der


def fetch_cert_sync(host: str, port: int = 443, timeout: float = 10.0, connect_ip: str | None = None) -> dict[str, Any]:
    """Blocking. Returns a dict with trusted/valid info. Never raises for TLS problems; records them.
    `connect_ip` connects to that address while still presenting/validating `host` (used for the DoH cross-check)."""
    result: dict[str, Any] = {"host": host, "port": port, "checked_at": time.time(), "trusted": False,
                              "error": None, "issuer": None, "issuer_org": None, "subject_org": None,
                              "san": [], "not_before_ts": None, "not_after_ts": None, "fingerprint_sha256": None,
                              "hostname_match": None, "self_signed": None, "expired": None, "trust_store": None}
    last_err = None
    for store in ("system", "certifi"):
        ctx = _ctx(store == "certifi")
        try:
            with socket.create_connection((connect_ip or host, port), timeout=timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    der = ssock.getpeercert(binary_form=True)
            result.update(_describe(cert, der))
            result["has_scts"] = has_embedded_scts(der)
            result["connect_ip"] = connect_ip
            result["trusted"] = True
            result["hostname_match"] = True
            result["trust_store"] = store
            return result
        except ssl.SSLCertVerificationError as e:
            last_err = e
            msg = str(e).lower()
            result["error"] = f"cert verification failed: {e.verify_message if hasattr(e, 'verify_message') else e}"
            result["self_signed"] = "self" in msg and "signed" in msg
            result["expired"] = "expired" in msg
            result["hostname_match"] = not ("hostname" in msg or "doesn't match" in msg or "does not match" in msg)
            # gather cert details without verification for reporting
            try:
                result.update(_unverified_details(host, port, timeout))
            except Exception:
                pass
            continue  # try the other store
        except (socket.timeout, TimeoutError) as e:
            result["error"] = f"timeout: {e}"
            return result
        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            result["error"] = f"connection error: {e}"
            return result
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
            return result
    if last_err is not None:
        result["trusted"] = False
    return result


def _describe(cert: dict, der: bytes) -> dict[str, Any]:
    subj = _flatten(cert.get("subject"))
    iss = _flatten(cert.get("issuer"))
    san = [v for (t, v) in cert.get("subjectAltName", ()) if t == "DNS"]
    return {
        "subject_cn": subj.get("commonName"),
        "subject_org": subj.get("organizationName"),
        "subject_country": subj.get("countryName"),
        "issuer": iss.get("commonName"),
        "issuer_org": iss.get("organizationName"),
        "san": san,
        "not_before_ts": _parse_ts(cert["notBefore"]) if cert.get("notBefore") else None,
        "not_after_ts": _parse_ts(cert["notAfter"]) if cert.get("notAfter") else None,
        "fingerprint_sha256": hashlib.sha256(der).hexdigest(),
        "validation_level": "OV/EV" if subj.get("organizationName") else "DV",
    }


def _unverified_details(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Fetch the leaf certificate without verification, purely for reporting. Requires cryptography if present."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            der = ssock.getpeercert(binary_form=True)
    out: dict[str, Any] = {"fingerprint_sha256": hashlib.sha256(der).hexdigest()}
    try:
        from cryptography import x509  # optional
        from cryptography.x509.oid import NameOID, ExtensionOID
        c = x509.load_der_x509_certificate(der)
        def _attr(name, oid):
            v = name.get_attributes_for_oid(oid)
            return v[0].value if v else None
        out["subject_cn"] = _attr(c.subject, NameOID.COMMON_NAME)
        out["subject_org"] = _attr(c.subject, NameOID.ORGANIZATION_NAME)
        out["issuer"] = _attr(c.issuer, NameOID.COMMON_NAME)
        out["issuer_org"] = _attr(c.issuer, NameOID.ORGANIZATION_NAME)
        out["not_before_ts"] = c.not_valid_before_utc.timestamp()
        out["not_after_ts"] = c.not_valid_after_utc.timestamp()
        try:
            out["san"] = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
        except Exception:
            out["san"] = []
        out["self_signed"] = c.issuer == c.subject
    except ImportError:
        pass
    return out


async def fetch_cert(host: str, port: int = 443, timeout: float = 10.0, connect_ip: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(fetch_cert_sync, host, port, timeout, connect_ip)
