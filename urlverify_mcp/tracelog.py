"""Full data log: every MCP request/response, LLM request/response (incl. reasoning), search/fetch exchange,
structured API result, L0 result and rules decision, in chronological JSONL. Off by default.

Files: <dir>/full-YYYYMMDDHHMMSS.log, rotated when the current file exceeds max_bytes."""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("urlverify_trace_id", default="-")


def set_trace_id(trace_id: str) -> contextvars.Token:
    return _trace_id.set(trace_id)


def reset_trace_id(token: contextvars.Token) -> None:
    _trace_id.reset(token)


class TraceLog:
    def __init__(self, enabled: bool = False, directory: str = "~/.urlverify_mcp/logs", max_bytes: int = 1_048_576):
        self.enabled = enabled
        self.dir = Path(os.path.expanduser(directory))
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fh = None
        self._path: Path | None = None
        self._seq = 0

    # ---- configuration (admin UI may flip this at runtime)
    def configure(self, enabled: bool, directory: str | None = None, max_bytes: int | None = None) -> None:
        with self._lock:
            self.enabled = enabled
            if directory:
                self.dir = Path(os.path.expanduser(directory))
            if max_bytes:
                self.max_bytes = max_bytes
            if not enabled and self._fh:
                self._fh.close()
                self._fh = None
                self._path = None

    # ---- file handling
    def _open_new(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        path = self.dir / f"full-{stamp}.log"
        n = 1
        while path.exists():           # two rotations within the same second
            path = self.dir / f"full-{stamp}-{n}.log"
            n += 1
        self._fh = open(path, "a", encoding="utf-8")
        self._path = path

    def _ensure(self) -> None:
        if self._fh is None or self._path is None:
            self._open_new()
            return
        try:
            if self._path.stat().st_size >= self.max_bytes:
                self._fh.close()
                self._open_new()
        except FileNotFoundError:
            self._fh.close()
            self._open_new()

    # ---- writing
    def log(self, kind: str, **fields: Any) -> None:
        """Append one record. Never raises (logging must not break verification)."""
        if not self.enabled:
            return
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "t": round(time.time(), 3),
               "trace_id": _trace_id.get(), "kind": kind}
        rec.update(fields)
        try:
            line = json.dumps(rec, ensure_ascii=False, default=_jsonable)
        except Exception as e:  # noqa: BLE001
            line = json.dumps({"ts": rec["ts"], "trace_id": rec["trace_id"], "kind": kind, "error": f"unserializable: {e}"})
        with self._lock:
            try:
                self._ensure()
                self._seq += 1
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:  # noqa: BLE001
                pass

    # ---- inspection (admin UI)
    def files(self) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob("full-*.log"), reverse=True):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime, "current": p == self._path})
        return out

    def read(self, name: str, tail_bytes: int = 262_144) -> str:
        p = self.dir / name
        if not p.is_file() or not name.startswith("full-") or "/" in name or "\\" in name:
            raise FileNotFoundError(name)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                data = f.read()
                data = data[data.find(b"\n") + 1:]
            else:
                data = f.read()
        return data.decode("utf-8", errors="replace")

    def current_path(self) -> str | None:
        return str(self._path) if self._path else None


def _jsonable(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    return repr(o)


TRACE = TraceLog()


def configure_from(cfg) -> TraceLog:
    fl = cfg.full_log
    TRACE.configure(fl.enabled, fl.dir, fl.max_bytes)
    return TRACE
