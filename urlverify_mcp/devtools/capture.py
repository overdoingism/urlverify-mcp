"""Decision-input capture for offline replay tests (developer tool, off unless URLVERIFY_CAPTURE_DIR is set).

Everything the rules engine sees is written to one gzip'd JSON file per verification: the L0 result, the investigator's
submission, the raw tool outputs it cited (evidence store), aging, provenance and registry state. tests/test_replay.py
feeds these back into rules.decide() so a rule change can be checked against real cases in seconds instead of re-running
a four-minute live verification. Secrets (API keys, tokens) are never captured.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import Any

CAPTURED_CONFIG = ("identity", "lists", "injection_patterns", "package_registry_fast_path")


def capture_dir() -> Path | None:
    d = os.environ.get("URLVERIFY_CAPTURE_DIR", "").strip()
    return Path(d) if d else None


def capture(trace_id: str, project: str, url: str, cfg, l0, sub, store: dict[str, str], cached, ages, target_age,
            provenance, registry_state, decision) -> Path | None:
    d = capture_dir()
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    conf = {k: v for k, v in cfg.model_dump(mode="json").items() if k in CAPTURED_CONFIG}
    conf.get("identity", {}).pop("github_token", None)
    slug = re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-")[:40] or "case"
    data: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "trace_id": trace_id,
        "project": project, "url": url, "config": conf,
        "l0": l0.model_dump(mode="json", exclude={"fetched_target_text"}),
        "submission": sub.model_dump(mode="json"), "store": store, "cached_identity": cached,
        "ages": ages, "target_domain_age": target_age, "provenance": provenance,
        "registry_state": {k: v for k, v in (registry_state or {}).items()} if registry_state else None,
        "observed": {"verdict": decision.verdict.value, "confidence": round(decision.confidence, 3), "notes": decision.notes},
        # filled in by hand after review: what the rules SHOULD decide for this case
        "expect": None,
    }
    path = d / f"{slug}-{trace_id}.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    return path


def load(path: str | Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
