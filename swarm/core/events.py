"""In-process pub/sub used by every subsystem to report what it is doing.

Events are plain dicts with a ``type`` and a ``ts``; subscribers get them in
publication order. The service layer forwards them to TUI clients and the
telemetry log persists the interesting ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from typing import Any

from swarm.core.types import now_iso

log = logging.getLogger(__name__)

Event = dict[str, Any]
Subscriber = Callable[[Event], Any]


class EventBus:
    def __init__(self, history: int = 500) -> None:
        self._subs: list[Subscriber] = []
        self._queues: list[asyncio.Queue[Event]] = []
        self.history: deque[Event] = deque(maxlen=history)

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        self._subs.append(fn)

        def unsubscribe() -> None:
            if fn in self._subs:
                self._subs.remove(fn)

        return unsubscribe

    def queue(self, maxsize: int = 1000) -> asyncio.Queue[Event]:
        """Return a queue that receives every future event. Drop when full."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._queues.append(q)
        return q

    def release_queue(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, type_: str, **data: Any) -> Event:
        event: Event = {"type": type_, "ts": now_iso(), **data}
        self.history.append(event)
        for fn in list(self._subs):
            try:
                result = fn(event)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001 - a bad subscriber must not break publishers
                log.exception("event subscriber failed")
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event
