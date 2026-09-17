"""Observed dependency health. Every real call to an external dependency reports success or failure here; nothing is
probed. The table is a REPORT for humans (admin UI, logs, the `degraded` field of a result) and is never used to
decide whether a dependency is enabled: networks flap, and the next call may well succeed."""
from __future__ import annotations

import contextvars
import sys
import threading
import time
from typing import Any

from .tracelog import TRACE

ESCALATE_AT = 3   # consecutive failures that trigger the louder log line

_degraded: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("urlverify_degraded", default=None)


class Health:
    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, dict[str, Any]] = {}
        self._store = None

    def attach(self, store) -> None:
        """Bind a Storage so observations persist across restarts (loads what is already there)."""
        self._store = store
        try:
            for row in store.load_health():
                self._state[row["dep"]] = row
        except Exception:  # noqa: BLE001
            pass

    def observe(self, dep: str, ok: bool, error: str | None = None) -> None:
        now = time.time()
        with self._lock:
            st = self._state.setdefault(dep, {"dep": dep, "last_ok": None, "last_fail": None, "last_error": None,
                                              "consecutive_fail": 0, "ok_count": 0, "fail_count": 0})
            if ok:
                was_down = st["consecutive_fail"] >= ESCALATE_AT
                st.update(last_ok=now, consecutive_fail=0, ok_count=st["ok_count"] + 1)
                if was_down:
                    print(f"!! DEPENDENCY {dep}: recovered", file=sys.stderr, flush=True)
                    TRACE.log("dependency_recovered", dep=dep)
            else:
                st.update(last_fail=now, last_error=(error or "")[:300], consecutive_fail=st["consecutive_fail"] + 1,
                          fail_count=st["fail_count"] + 1)
                n = st["consecutive_fail"]
                tag = "!! DEPENDENCY" if n < ESCALATE_AT else "!!! DEPENDENCY DOWN"
                print(f"{tag} {dep}: {st['last_error']} ({n} consecutive)", file=sys.stderr, flush=True)
                TRACE.log("dependency_failure", dep=dep, error=st["last_error"], consecutive=n)
                bag = _degraded.get()
                if bag is not None:
                    bag.add(dep)
            snapshot = dict(st)
        if self._store is not None:
            try:
                self._store.save_health(snapshot)
            except Exception:  # noqa: BLE001
                pass

    def table(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted((dict(v) for v in self._state.values()), key=lambda r: r["dep"])


HEALTH = Health()


def observe(dep: str, ok: bool, error: str | None = None) -> None:
    HEALTH.observe(dep, ok, error)


def begin_collect() -> contextvars.Token:
    return _degraded.set(set())


def end_collect(token: contextvars.Token) -> list[str]:
    bag = _degraded.get() or set()
    _degraded.reset(token)
    return sorted(bag)


def result_is_ok(r: dict | None) -> bool:
    """Convention used by the structured helpers: {"ok": False, "error": ...} marks a dependency failure."""
    return bool(r) and r.get("ok") is not False
