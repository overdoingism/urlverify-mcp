"""Fetched text and its tool-assigned provenance; never assigned by the LLM."""
from __future__ import annotations


class EvidenceStore(dict[str, str]):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kinds: dict[str, str] = {}

    def __setitem__(self, key: str, value: str) -> None:
        self.kinds.pop(key, None)
        super().__setitem__(key, value)

    def record(self, source: str, text: str, kind: str) -> None:
        self[source] = text
        self.kinds[source] = kind


def record_kind(store: dict[str, str], source: str) -> str | None:
    return store.kinds.get(source) if isinstance(store, EvidenceStore) else None
