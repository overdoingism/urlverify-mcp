"""Everything the investigator fetched, kept apart by what it is (AGENTS.md §6.2, typed facts):

- structured records: JSON returned by our own API tools (GitHub, Hugging Face, Wikidata, Wikipedia, Wayback,
  registries). The program flattens each record into numbered facts (`F12 github.repo.full_name = ggml-org/llama.cpp`)
  that the LLM cites by ID; it never has to copy JSON, and the rules never have to check a copy.
- pages: text of fetched web pages. Evidence from pages still needs a verbatim quote.

A structured record and a page fetched from the same URL are separate entries and never overwrite each other. A record
can be cited by any of its URLs (api.github.com/repos/o/r and github.com/o/r are the same record).

The dict view (store[url] -> text) is kept for the rules helpers: it returns the structured record's JSON when one
exists for that URL, else the page text. Provenance (kind, facts) is assigned by tools, never by the LLM.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_FACTS_PER_RECORD = 120
MAX_FACT_VALUE = 300
_SKIP_KEYS = {"ok", "found", "source"}


def norm_url(u: str) -> str:
    """Comparable form of a URL: no scheme / www / query / fragment / trailing slash, lower-case, and API URLs mapped to
    the page they describe."""
    s = (u or "").strip()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s, flags=re.I).split("#", 1)[0]
    s = s.split("?", 1)[0].rstrip("/").lower()
    s = s[4:] if s.startswith("www.") else s
    rules = [(r"^api\.github\.com/repos/([^/]+)/([^/]+).*$", r"github.com/\1/\2"),
             (r"^api\.github\.com/(?:users|orgs)/([^/]+)$", r"github.com/\1"),
             (r"^huggingface\.co/api/models/([^/]+)/([^/]+).*$", r"huggingface.co/\1/\2"),
             (r"^huggingface\.co/api/(datasets|spaces)/([^/]+)/([^/]+).*$", r"huggingface.co/\1/\2/\3"),
             (r"^huggingface\.co/api/(?:organizations|users)/([^/]+)(?:/overview)?$", r"huggingface.co/\1"),
             (r"^hf\.co/", "huggingface.co/"),
             (r"^pypi\.org/pypi/([^/]+)(?:/([^/]+))?/json$", r"pypi.org/project/\1"),
             (r"^registry\.npmjs\.org/(.+)$", r"npmjs.com/package/\1"),
             (r"^wikidata\.org/wiki/special:entitydata/(q\d+)(?:\.json)?$", r"wikidata.org/wiki/\1"),
             (r"^(\w+)\.m\.wikipedia\.org/", r"\1.wikipedia.org/")]
    for pat, rep in rules:
        s = re.sub(pat, rep, s)
    return s.replace("%2f", "/")


def flatten(obj: Any, prefix: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(o: Any, path: str) -> None:
        if len(out) >= MAX_FACTS_PER_RECORD:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if path == prefix and k in _SKIP_KEYS:
                    continue
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o[:20]):
                walk(v, f"{path}[{i}]")
        elif o is None or o == "":
            return
        else:
            v = "true" if o is True else "false" if o is False else str(o)
            out.append((path, v if len(v) <= MAX_FACT_VALUE else v[:MAX_FACT_VALUE] + "…"))
    walk(obj, prefix)
    return out


class EvidenceStore(dict[str, str]):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kinds: dict[str, str] = {}              # structured record source -> kind
        self.pages: dict[str, str] = dict(self)      # page url -> text (never shadowed by a record)
        self.aliases: dict[str, str] = {}            # norm_url -> record source
        self.page_index: dict[str, str] = {norm_url(k): k for k in self.pages}
        self.facts: dict[str, tuple[str, str, str]] = {}   # fact id -> (record source, path, value)
        self.record_ids: dict[str, str] = {}         # record source -> R#

    # ---------------------------------------------------------------- pages
    def __setitem__(self, key: str, value: str) -> None:
        """Store a PAGE. Never overwrites a structured record for the same URL (the dict view keeps the record)."""
        self.pages[key] = value
        self.page_index[norm_url(key)] = key
        if key not in self.kinds:
            super().__setitem__(key, value)

    def page_text(self, url: str) -> str | None:
        if url in self.pages:
            return self.pages[url]
        k = self.page_index.get(norm_url(url))
        return self.pages.get(k) if k else None

    # ---------------------------------------------------------------- structured records
    def record(self, source: str, text: str, kind: str, aliases: list[str] | None = None) -> list[str]:
        """Store a structured record and derive its facts. Returns the new fact ids."""
        super().__setitem__(source, text)
        self.kinds[source] = kind
        for a in [source] + list(aliases or []):
            if a:
                self.aliases[norm_url(a)] = source
        if source not in self.record_ids:
            self.record_ids[source] = f"R{len(self.record_ids) + 1}"
        for fid in [f for f, (src, _, _) in self.facts.items() if src == source]:
            del self.facts[fid]                       # a refreshed record replaces its old facts
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return []
        ids = []
        for path, value in flatten(data, kind):
            fid = f"F{len(self.facts) + 1}"
            while fid in self.facts:
                fid = f"F{int(fid[1:]) + 1}"
            self.facts[fid] = (source, path, value)
            ids.append(fid)
        return ids

    def find_record(self, cite: str) -> str | None:
        """Record source for a citation: a record id (R3), a fact id (F12), or any URL of the record."""
        c = (cite or "").strip()
        if c in self.kinds:
            return c
        if re.fullmatch(r"R\d+", c):
            return next((s for s, r in self.record_ids.items() if r == c), None)
        if re.fullmatch(r"F\d+", c):
            return self.facts.get(c, (None,))[0]
        return self.aliases.get(norm_url(c))

    def fact_text(self, fids: list[str]) -> str:
        return "; ".join(f"{self.facts[f][1]} = {self.facts[f][2]}" for f in fids if f in self.facts)

    def render(self, source: str, title: str) -> str:
        """What the LLM sees for a structured record: numbered facts, no JSON to copy."""
        rid = self.record_ids.get(source, "?")
        lines = [f"RECORD {rid} · {title} · source {source}",
                 'Cite with {"kind": "' + self.kinds.get(source, "") + '", "facts": ["F..", ...], "claim": "..."}; do not copy values into a quote.']
        lines += [f"{fid} {path} = {value}" for fid, (src, path, value) in self.facts.items() if src == source]
        return "\n".join(lines)

    # ---------------------------------------------------------------- persistence (capture / replay)
    def to_json(self) -> dict[str, Any]:
        return {"format": "evidence-store/2", "pages": self.pages,
                "records": [{"source": s, "kind": k, "text": dict.get(self, s), "id": self.record_ids.get(s),
                             "aliases": [a for a, src in self.aliases.items() if src == s]} for s, k in self.kinds.items()],
                "facts": {f: list(v) for f, v in self.facts.items()}}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "EvidenceStore":
        if data.get("format") != "evidence-store/2":
            return cls(data)                          # legacy capture: plain url -> text
        st = cls()
        for url, text in (data.get("pages") or {}).items():
            st[url] = text
        for r in data.get("records") or []:
            dict.__setitem__(st, r["source"], r["text"])
            st.kinds[r["source"]] = r["kind"]
            st.record_ids[r["source"]] = r.get("id") or f"R{len(st.record_ids) + 1}"
            for a in r.get("aliases") or []:
                st.aliases[a] = r["source"]
        st.facts = {f: tuple(v) for f, v in (data.get("facts") or {}).items()}
        return st


def record_kind(store: dict[str, str], source: str) -> str | None:
    if not isinstance(store, EvidenceStore):
        return None
    rec = store.find_record(source)
    return store.kinds.get(rec) if rec else None
