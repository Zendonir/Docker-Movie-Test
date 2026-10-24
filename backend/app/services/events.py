"""Einfacher In-Process-Eventbus für Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._last_stats = 0.0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def _deliver(self, payload: str) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass  # langsamer Client verliert einzelne Fortschrittsmeldungen

    def publish(self, event: str, data: Any) -> None:
        payload = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(payload)
        else:
            loop.call_soon_threadsafe(self._deliver, payload)

    def stats_changed(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_stats > 2.0:
            self._last_stats = now
            self.publish("stats", {"ts": time.time()})


bus = EventBus()
