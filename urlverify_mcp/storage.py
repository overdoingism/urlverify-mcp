"""SQLite storage: three caches (anchors / certs / identity) + audit history + fetched-content store."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS cert_cache (
    host TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS identity_cache (
    key TEXT PRIMARY KEY, project TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS anchor_cache (
    etld1 TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS history (
    trace_id TEXT PRIMARY KEY, ts REAL NOT NULL, project TEXT, url TEXT, description TEXT,
    verdict TEXT, confidence REAL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dep_health (
    dep TEXT PRIMARY KEY, last_ok REAL, last_fail REAL, last_error TEXT, consecutive_fail INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0, fail_count INTEGER NOT NULL DEFAULT 0);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- generic helpers
    def _get(self, table: str, keycol: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE {keycol}=?", (key,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if "expires" in d and d["expires"] and d["expires"] < time.time():
            return None
        d["data"] = json.loads(d["data"])
        return d

    # ---- cert cache
    def get_cert(self, host: str) -> dict[str, Any] | None:
        r = self._get("cert_cache", "host", host.lower())
        return r["data"] if r else None

    def put_cert(self, host: str, data: dict[str, Any], ttl_s: float) -> None:
        now = time.time()
        exp = now + ttl_s
        # never cache past the certificate's own expiry
        if data.get("not_after_ts"):
            exp = min(exp, float(data["not_after_ts"]))
        self.conn.execute("REPLACE INTO cert_cache VALUES (?,?,?,?)", (host.lower(), json.dumps(data), exp, now))
        self.conn.commit()

    # ---- identity cache
    @staticmethod
    def identity_key(project: str) -> str:
        return " ".join(project.lower().split())

    def get_identity(self, project: str) -> dict[str, Any] | None:
        r = self._get("identity_cache", "key", self.identity_key(project))
        return r["data"] if r else None

    def put_identity(self, project: str, data: dict[str, Any], ttl_s: float) -> None:
        now = time.time()
        self.conn.execute("REPLACE INTO identity_cache VALUES (?,?,?,?,?)",
                          (self.identity_key(project), project, json.dumps(data), now + ttl_s, now))
        self.conn.commit()

    def invalidate_identity(self, key: str) -> None:
        self.conn.execute("DELETE FROM identity_cache WHERE key=?", (key,))
        self.conn.commit()

    # ---- anchors (observed data on top of the built-in seed)
    def get_anchor(self, etld1: str) -> dict[str, Any] | None:
        r = self._get("anchor_cache", "etld1", etld1)
        return r["data"] if r else None

    def put_anchor(self, etld1: str, data: dict[str, Any]) -> None:
        self.conn.execute("REPLACE INTO anchor_cache VALUES (?,?,?)", (etld1, json.dumps(data), time.time()))
        self.conn.commit()

    # ---- history
    def add_history(self, trace_id: str, project: str, url: str, description: str,
                    verdict: str, confidence: float, result: dict[str, Any]) -> None:
        self.conn.execute("REPLACE INTO history VALUES (?,?,?,?,?,?,?,?)",
                          (trace_id, time.time(), project, url, description, verdict, confidence, json.dumps(result)))
        self.conn.commit()

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT trace_id, ts, project, url, description, verdict, confidence FROM history ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_history(self, trace_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM history WHERE trace_id=?", (trace_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result"])
        return d

    # ---- listing for the admin UI
    def dump_table(self, table: str, limit: int = 500) -> list[dict[str, Any]]:
        assert table in {"cert_cache", "identity_cache", "anchor_cache"}
        rows = self.conn.execute(f"SELECT * FROM {table} ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    def delete_row(self, table: str, key: str) -> None:
        keycol = {"cert_cache": "host", "identity_cache": "key", "anchor_cache": "etld1"}[table]
        self.conn.execute(f"DELETE FROM {table} WHERE {keycol}=?", (key,))
        self.conn.commit()

    def clear_table(self, table: str) -> None:
        assert table in {"cert_cache", "identity_cache", "anchor_cache", "history"}
        self.conn.execute(f"DELETE FROM {table}")
        self.conn.commit()

    # ---- observed dependency health
    def load_health(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM dep_health").fetchall()]

    def save_health(self, row: dict[str, Any]) -> None:
        self.conn.execute("REPLACE INTO dep_health VALUES (?,?,?,?,?,?,?)",
                          (row["dep"], row.get("last_ok"), row.get("last_fail"), row.get("last_error"),
                           int(row.get("consecutive_fail") or 0), int(row.get("ok_count") or 0), int(row.get("fail_count") or 0)))
        self.conn.commit()

    # ---- kv
    def kv_get(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, k: str, v: str) -> None:
        self.conn.execute("REPLACE INTO kv VALUES (?,?)", (k, v))
        self.conn.commit()
