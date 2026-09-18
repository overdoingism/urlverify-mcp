"""Plain-text state store. Everything lives under one directory as human-readable JSON, so a user can inspect,
edit or delete any part of it (deleting a file resets that part). No database.

  <state>/cert_cache.json        host -> certificate facts (+ expiry)          } state: rebuildable caches,
  <state>/identity_cache.json    project key -> established identity (+ expiry) } safe to hand to someone else
  <state>/anchor_cache.json      observed platform-anchor data
  <log>/health.json              observed dependency health                    } log: records of what this
  <log>/history/index.jsonl      one line per verification (summary)          } installation did; may be
  <log>/history/<trace_id>.json  full result                                   } private, delete freely

Writes are atomic (temp file + rename). Two processes (serve + admin) may write the same cache file: each write
re-reads the file and merges its own change, so the worst case is losing a concurrent cache entry, never corrupting.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


class Storage:
    def __init__(self, directory: str | os.PathLike, log_dir: str | os.PathLike | None = None):
        self.dir = Path(os.path.expanduser(str(directory)))
        self.log_dir = Path(os.path.expanduser(str(log_dir))) if log_dir else self.dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / "history").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---- generic map files: {key: {"data": ..., "expires": ts|None, "updated": ts}}
    def _map(self, name: str) -> dict[str, Any]:
        return _read_json(self.dir / f"{name}.json", {})

    def _map_get(self, name: str, key: str) -> dict[str, Any] | None:
        row = self._map(name).get(key)
        if not row:
            return None
        if row.get("expires") and row["expires"] < time.time():
            return None
        return row

    def _map_put(self, name: str, key: str, data: Any, expires: float | None) -> None:
        with self._lock:
            m = self._map(name)                       # re-read: merge with what another process may have written
            m[key] = {"data": data, "expires": expires, "updated": time.time()}
            _atomic_write(self.dir / f"{name}.json", m)

    def _map_delete(self, name: str, key: str | None) -> None:
        with self._lock:
            if key is None:
                _atomic_write(self.dir / f"{name}.json", {})
                return
            m = self._map(name)
            m.pop(key, None)
            _atomic_write(self.dir / f"{name}.json", m)

    # ---- cert cache
    def get_cert(self, host: str) -> dict[str, Any] | None:
        r = self._map_get("cert_cache", host.lower())
        return r["data"] if r else None

    def put_cert(self, host: str, data: dict[str, Any], ttl_s: float) -> None:
        exp = time.time() + ttl_s
        if data.get("not_after_ts"):
            exp = min(exp, float(data["not_after_ts"]))          # never past the certificate's own expiry
        self._map_put("cert_cache", host.lower(), data, exp)

    # ---- identity cache
    @staticmethod
    def identity_key(project: str) -> str:
        return " ".join(project.lower().split())

    def get_identity(self, project: str) -> dict[str, Any] | None:
        r = self._map_get("identity_cache", self.identity_key(project))
        return r["data"] if r else None

    def put_identity(self, project: str, data: dict[str, Any], ttl_s: float) -> None:
        data = dict(data)
        data.setdefault("project", project)
        self._map_put("identity_cache", self.identity_key(project), data, time.time() + ttl_s)

    def invalidate_identity(self, key: str) -> None:
        self._map_delete("identity_cache", key)

    # ---- anchors (observed data on top of the built-in seed)
    def get_anchor(self, etld1: str) -> dict[str, Any] | None:
        r = self._map_get("anchor_cache", etld1)
        return r["data"] if r else None

    def put_anchor(self, etld1: str, data: dict[str, Any]) -> None:
        self._map_put("anchor_cache", etld1, data, None)

    # ---- history: one file per verification + an index line
    def add_history(self, trace_id: str, project: str, url: str, description: str,
                    verdict: str, confidence: float, result: dict[str, Any]) -> None:
        _atomic_write(self.log_dir / "history" / f"{trace_id}.json", result)
        line = {"trace_id": trace_id, "ts": time.time(), "project": project, "url": url, "description": description,
                "verdict": verdict, "confidence": confidence}
        with self._lock, open(self.log_dir / "history" / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        p = self.log_dir / "history" / "index.jsonl"
        if not p.is_file():
            return []
        rows: dict[str, dict[str, Any]] = {}
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    rows[r["trace_id"]] = r                       # a re-added trace_id keeps its latest line
                except (json.JSONDecodeError, KeyError):
                    continue
        return sorted(rows.values(), key=lambda r: r["ts"], reverse=True)[:limit]

    def clear_history(self) -> int:
        """Delete every history record (index + per-trace files). Returns the number of files removed."""
        d = self.log_dir / "history"
        n = 0
        with self._lock:
            for f in d.glob("*.json"):
                try:
                    f.unlink(); n += 1
                except OSError:
                    pass
            idx = d / "index.jsonl"
            if idx.exists():
                idx.unlink(); n += 1
        return n

    def get_history(self, trace_id: str) -> dict[str, Any] | None:
        if not trace_id or "/" in trace_id or "\\" in trace_id or trace_id.startswith("."):
            return None
        result = _read_json(self.log_dir / "history" / f"{trace_id}.json", None)
        if result is None:
            return None
        meta = next((r for r in self.list_history(10_000) if r["trace_id"] == trace_id), {})
        return {**meta, "trace_id": trace_id, "result": result}

    # ---- listing for the admin UI
    def dump_table(self, table: str, limit: int = 500) -> list[dict[str, Any]]:
        assert table in {"cert_cache", "identity_cache", "anchor_cache"}
        keycol = {"cert_cache": "host", "identity_cache": "key", "anchor_cache": "etld1"}[table]
        out = []
        for k, row in self._map(table).items():
            d = {keycol: k, "data": row.get("data"), "expires": row.get("expires"), "updated": row.get("updated")}
            if table == "identity_cache":
                d["project"] = (row.get("data") or {}).get("project", k)
            out.append(d)
        out.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        return out[:limit]

    def delete_row(self, table: str, key: str) -> None:
        self._map_delete(table, key)

    def clear_table(self, table: str) -> None:
        assert table in {"cert_cache", "identity_cache", "anchor_cache", "history"}
        if table == "history":
            with self._lock:
                for p in (self.log_dir / "history").glob("*.json"):
                    p.unlink()
                idx = self.log_dir / "history" / "index.jsonl"
                if idx.exists():
                    idx.unlink()
            return
        self._map_delete(table, None)

    # ---- observed dependency health: {dep: row}
    def load_health(self) -> list[dict[str, Any]]:
        return list(_read_json(self.log_dir / "health.json", {}).values())

    def save_health(self, row: dict[str, Any]) -> None:
        with self._lock:
            m = _read_json(self.log_dir / "health.json", {})
            m[row["dep"]] = row
            _atomic_write(self.log_dir / "health.json", m)

    # ---- kv (small settings, e.g. remembered probes)
    def kv_get(self, k: str) -> str | None:
        return (_read_json(self.dir / "kv.json", {}) or {}).get(k)

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            m = _read_json(self.dir / "kv.json", {})
            m[k] = v
            _atomic_write(self.dir / "kv.json", m)
