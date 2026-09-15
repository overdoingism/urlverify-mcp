"""URL normalisation, eTLD+1, punycode / homoglyph / typosquat / subdomain-abuse detection."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import idna
import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)  # offline PSL snapshot

SHORTENERS = {
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "cutt.ly", "rb.gy",
    "t.ly", "shorturl.at", "tiny.cc", "lnkd.in", "s.id", "rebrand.ly", "bl.ink", "u.to", "v.gd",
}

# Common visual confusables -> ascii (subset; covers the practical attacks)
CONFUSABLES = {
    "0": "o", "1": "l", "ⅼ": "l", "ӏ": "l", "ǀ": "l", "І": "i", "і": "i", "ı": "i", "ɩ": "i",
    "о": "o", "ο": "o", "օ": "o", "а": "a", "α": "a", "ａ": "a", "е": "e", "ε": "e", "ё": "e",
    "р": "p", "ρ": "p", "с": "c", "ϲ": "c", "ς": "c", "у": "y", "γ": "y", "х": "x", "χ": "x",
    "ԁ": "d", "ԛ": "q", "ѕ": "s", "ʜ": "h", "һ": "h", "ո": "n", "ռ": "n", "ｍ": "m", "ѵ": "v", "ν": "v",
    "ԝ": "w", "ω": "w", "ϳ": "j", "ј": "j", "ｇ": "g", "ɡ": "g", "ｋ": "k", "κ": "k", "ｔ": "t", "τ": "t",
    "ｂ": "b", "Ь": "b", "ｚ": "z", "ᴢ": "z", "rn": "m", "vv": "w", "cl": "d",
}


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        host_ascii = idna.encode(host, uts46=True).decode("ascii")
    except Exception:
        host_ascii = host.lower()
    netloc = host_ascii
    if parts.port and not ((parts.scheme == "https" and parts.port == 443) or (parts.scheme == "http" and parts.port == 80)):
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def etld1_of(host_or_url: str) -> str:
    host = host_of(host_or_url) if "://" in host_or_url else host_or_url.lower()
    ext = _extract(host)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return host


def unicode_host(host_ascii: str) -> str:
    try:
        return idna.decode(host_ascii)
    except Exception:
        return host_ascii


def skeleton(label: str) -> str:
    """Approximate unicode-confusable skeleton for a hostname label."""
    s = unicodedata.normalize("NFKD", label).lower()
    out = []
    for ch in s:
        if unicodedata.combining(ch):
            continue
        out.append(CONFUSABLES.get(ch, ch))
    r = "".join(out)
    for multi, rep in (("rn", "m"), ("vv", "w"), ("cl", "d")):
        r = r.replace(multi, rep)
    return r


def scripts_in(label: str) -> set[str]:
    scripts = set()
    for ch in label:
        if ch in "-.0123456789":
            continue
        name = unicodedata.name(ch, "")
        first = name.split(" ")[0] if name else "UNKNOWN"
        scripts.add(first if first in {"LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "HEBREW", "ARABIC", "CJK", "HIRAGANA", "KATAKANA", "HANGUL"} else ("LATIN" if ch.isascii() else first))
    return scripts


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def analyse_host(host_ascii: str, known_domains: list[str]) -> dict:
    """Return structural findings about a host relative to a list of known official / platform eTLD+1s."""
    findings: dict = {"punycode": False, "mixed_script": False, "confusable_of": None,
                      "typosquat_of": None, "subdomain_abuse_of": None, "shortener": False, "ip_literal": False}
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host_ascii) or ":" in host_ascii:
        findings["ip_literal"] = True
        return findings
    uni = unicode_host(host_ascii)
    if any(lbl.startswith("xn--") for lbl in host_ascii.split(".")):
        findings["punycode"] = True
    labels = uni.split(".")
    for lbl in labels:
        sc = scripts_in(lbl)
        if len(sc) > 1:
            findings["mixed_script"] = True
    e1 = etld1_of(host_ascii)
    if e1 in SHORTENERS:
        findings["shortener"] = True
    reg_label = e1.split(".")[0]
    reg_skel = skeleton(unicode_host(reg_label))
    for kd in known_domains:
        kd = kd.lower()
        if kd == e1:
            continue
        kd_label = kd.split(".")[0]
        # confusable: skeleton identical but string differs
        if reg_skel == skeleton(kd_label) and reg_label != kd_label:
            findings["confusable_of"] = kd
        # typosquat: small edit distance on a reasonably long label
        elif len(kd_label) >= 5 and 0 < levenshtein(reg_label, kd_label) <= (1 if len(kd_label) < 8 else 2):
            findings["typosquat_of"] = kd
        # subdomain abuse: official domain appears as a label prefix but eTLD+1 differs
        if host_ascii != kd and (host_ascii.startswith(kd + ".") or ("." + kd + ".") in host_ascii) and e1 != kd:
            findings["subdomain_abuse_of"] = kd
        # also: official brand label inside a hyphenated registrable label (lmstudio-download.com)
        if kd_label != reg_label and len(kd_label) >= 5 and re.search(rf"(^|-){re.escape(kd_label)}(-|$)", reg_label):
            findings.setdefault("brand_in_label_of", kd)
            findings["brand_in_label_of"] = kd
    return findings
