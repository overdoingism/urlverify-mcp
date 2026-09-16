"""Progress reporting to the MCP client (notifications/progress) plus a heartbeat.

Why a heartbeat is safe here: every network wait in the pipeline has its own timeout and the whole verification
runs under budget.max_total_s, so the heartbeat's lifetime is bounded by the verification task's lifetime."""
from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, Awaitable, Callable

Reporter = Callable[[float, float | None, str | None], Awaitable[Any]]

_current: contextvars.ContextVar["Progress | None"] = contextvars.ContextVar("urlverify_progress", default=None)


class Progress:
    def __init__(self, reporter: Reporter | None, events: bool = True, heartbeat_s: int = 15):
        self.reporter = reporter
        self.events = events
        self.heartbeat_s = heartbeat_s
        self.started = time.time()
        self.last_message = "starting"
        self.last_fraction = 0.0
        self.last_sent = 0.0
        self.sent = 0
        self._hb: asyncio.Task | None = None

    async def report(self, message: str, fraction: float | None = None) -> None:
        """Record the current stage; forward to the client when events are enabled."""
        self.last_message = message
        if fraction is not None:
            self.last_fraction = max(self.last_fraction, min(1.0, fraction))
        if self.reporter and self.events:
            await self._send(message)

    async def _send(self, message: str) -> None:
        try:
            await self.reporter(round(self.last_fraction * 100, 1), 100.0, message)
            self.sent += 1
            self.last_sent = time.time()
        except Exception:  # noqa: BLE001  (a client that cannot take progress must not break verification)
            pass

    def start_heartbeat(self) -> None:
        if self.reporter and self.heartbeat_s > 0 and self._hb is None:
            self._hb = asyncio.create_task(self._beat(), name="urlverify-heartbeat")

    async def _beat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                elapsed = int(time.time() - self.started)
                await self._send(f"{self.last_message} (still working, {elapsed}s elapsed)")
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        if self._hb is not None:
            self._hb.cancel()
            try:
                await self._hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._hb = None

    def stage(self) -> str:
        return f"{self.last_message} at {int(self.last_fraction * 100)}%"


def current() -> Progress | None:
    return _current.get()


async def report(message: str, fraction: float | None = None) -> None:
    p = _current.get()
    if p is not None:
        await p.report(message, fraction)


def bind(p: Progress | None) -> contextvars.Token:
    return _current.set(p)


def unbind(token: contextvars.Token) -> None:
    _current.reset(token)
