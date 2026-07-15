"""Bounded FIFO main-thread read queue for the secure HoudiniBridge.

A self-contained, fully testable queue that does NOT import ``hou``, ``rpyc``,
the Runtime, SQLite, or any UI. It owns no background worker and creates no
asyncio tasks: the owner/main thread drives execution explicitly via
:meth:`MainThreadReadQueue.pump_one`.

Threading model: a transport thread calls :meth:`submit` (which has a running
event loop) and awaits the returned future; the Houdini main thread calls
:meth:`pump_one` to run exactly one queued operation synchronously and resolve
that future. A queued item whose deadline elapses before it starts is expired
without running; a queued item cancelled before it starts is resolved as
cancelled without running; a running operation is never interrupted, but if it
is cancelled mid-run its (successful) result is discarded.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class QueueItemState(StrEnum):
    """Lifecycle of a single queued read."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class QueueItem:
    """Public, immutable snapshot of a queued request."""

    request_id: str
    deadline_monotonic: float


class QueueError(Exception):
    """Base class for main-thread queue errors."""


class QueueFull(QueueError):
    """Raised by :meth:`submit` when the queue is at capacity."""


class QueueRejected(QueueError):
    """A submission was rejected (shutdown / duplicate), or a queued item was
    drained on shutdown."""


class QueueItemExpired(QueueError):
    """Resolved onto a future when its deadline elapses before it starts."""


class QueueItemCancelled(QueueError):
    """Resolved onto a future when a request is cancelled before or during run."""


class _Entry:
    """Internal bookkeeping for one queued item."""

    __slots__ = ("item", "operation", "future", "discard")

    def __init__(
        self,
        item: QueueItem,
        operation: Callable[[], object],
        future: asyncio.Future,
    ) -> None:
        self.item = item
        self.operation = operation
        self.future = future
        self.discard = False


class MainThreadReadQueue:
    """A bounded FIFO queue drained explicitly by the owning (main) thread."""

    def __init__(self, *, capacity: int = 64) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._pending: deque[_Entry] = deque()
        self._running: _Entry | None = None
        self._shutdown = False

    def submit(
        self,
        request_id: str,
        operation: Callable[[], object],
        *,
        deadline_monotonic: float,
    ) -> Awaitable[object]:
        """Enqueue a read and return an awaitable for its result.

        Rejects (raises) immediately when shut down, when ``request_id`` is
        already pending, or when the queue is at capacity. The returned
        awaitable is resolved later by :meth:`pump_one` (or :meth:`cancel` /
        :meth:`shutdown`).
        """
        if type(request_id) is not str:
            raise TypeError("request_id must be a string")
        if not request_id:
            raise ValueError("request_id must be a non-empty string")
        if self._shutdown:
            raise QueueRejected("queue is shut down")
        if self._is_pending(request_id):
            raise QueueRejected(f"request_id {request_id!r} is already pending")
        if len(self._pending) >= self._capacity:
            raise QueueFull("queue is at capacity")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = QueueItem(
            request_id=request_id, deadline_monotonic=deadline_monotonic
        )
        self._pending.append(_Entry(item, operation, future))
        return future

    def pump_one(self, *, now: float | None = None) -> bool:
        """Run exactly one queued operation on the calling (owner) thread.

        Returns ``True`` if an item was consumed (run, expired, or otherwise
        resolved), ``False`` if the queue was empty. At most one item is ever
        ``RUNNING`` at a time, and only on this thread.
        """
        if now is None:
            now = time.monotonic()
        if not self._pending:
            return False
        entry = self._pending.popleft()
        if entry.item.deadline_monotonic <= now:
            # Deadline elapsed before the item could start: do not run it.
            entry.future.set_exception(
                QueueItemExpired("deadline exceeded before start")
            )
            return True
        self._running = entry
        try:
            try:
                result = entry.operation()
            except Exception as exc:
                # Operation failures must reach the awaiter, never swallowed.
                entry.future.set_exception(exc)
                return True
            if entry.discard:
                # Cancelled mid-run: the operation finished, but discard the
                # result so a cancelled caller never observes it.
                entry.future.set_exception(
                    QueueItemCancelled("cancelled during run; result discarded")
                )
                return True
            entry.future.set_result(result)
            return True
        finally:
            self._running = None

    def cancel(self, request_id: str) -> bool:
        """Cancel a pending or running request by id.

        A queued item is removed and resolved as cancelled without running. A
        running item cannot be interrupted; instead its result is discarded when
        the operation completes. Returns ``True`` if the id was found.
        """
        if self._running is not None and self._running.item.request_id == request_id:
            self._running.discard = True
            return True
        remaining: deque[_Entry] = deque()
        found = False
        for entry in self._pending:
            if not found and entry.item.request_id == request_id:
                found = True
                entry.future.set_exception(
                    QueueItemCancelled("cancelled before start")
                )
                continue
            remaining.append(entry)
        self._pending = remaining
        return found

    def shutdown(self) -> None:
        """Stop accepting new work and reject all queued items.

        Idempotent. Leaves no pending waiter behind.
        """
        if self._shutdown:
            return
        self._shutdown = True
        while self._pending:
            entry = self._pending.popleft()
            entry.future.set_exception(QueueRejected("queue shut down"))

    @property
    def pending_count(self) -> int:
        """Number of items not yet in a terminal state (queued or running)."""
        return len(self._pending) + (1 if self._running is not None else 0)

    def _is_pending(self, request_id: str) -> bool:
        if self._running is not None and self._running.item.request_id == request_id:
            return True
        return any(entry.item.request_id == request_id for entry in self._pending)
