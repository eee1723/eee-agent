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

Cross-thread safety: every future lives on the transport thread's event loop,
while :meth:`pump_one`/:meth:`cancel`/:meth:`shutdown` run on the Houdini main
thread. Future completion is therefore scheduled through the owning loop's
``call_soon_threadsafe`` (never a direct ``set_result``/``set_exception`` from
another thread), and all queue bookkeeping (``_pending``/``_running``/
``_shutdown``/``discard``) is guarded by a single lock. The operation itself
still runs only on the ``pump_one`` thread, outside the lock.
"""

from __future__ import annotations

import asyncio
import threading
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


# Sentinels for "no result provided" (an operation may legitimately return None).
_UNSET = object()


def _set_result_safely(future: asyncio.Future, result: object) -> None:
    """Resolve ``future`` with ``result`` unless it is already done.

    Runs ON the owning loop thread (scheduled via ``call_soon_threadsafe``), so
    the ``done()`` check is authoritative and never races with itself.
    """
    if not future.done():
        future.set_result(result)


def _set_exception_safely(future: asyncio.Future, exception: BaseException) -> None:
    """Resolve ``future`` with ``exception`` unless it is already done."""
    if not future.done():
        future.set_exception(exception)


class _Entry:
    """Internal bookkeeping for one queued item."""

    __slots__ = ("item", "operation", "future", "loop", "discard")

    def __init__(
        self,
        item: QueueItem,
        operation: Callable[[], object],
        future: asyncio.Future,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.item = item
        self.operation = operation
        self.future = future
        self.loop = loop
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
        self._lock = threading.Lock()

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
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._shutdown:
                raise QueueRejected("queue is shut down")
            if self._is_pending_locked(request_id):
                raise QueueRejected(f"request_id {request_id!r} is already pending")
            if len(self._pending) >= self._capacity:
                raise QueueFull("queue is at capacity")
            future: asyncio.Future = loop.create_future()
            item = QueueItem(
                request_id=request_id, deadline_monotonic=deadline_monotonic
            )
            self._pending.append(_Entry(item, operation, future, loop))
        return future

    def pump_one(self, *, now: float | None = None) -> bool:
        """Run exactly one queued operation on the calling (owner) thread.

        Returns ``True`` if an item was consumed (run, expired, or otherwise
        resolved), ``False`` if the queue was empty. At most one item is ever
        ``RUNNING`` at a time, and only on this thread. The operation runs
        outside the lock; its future is resolved through the owning loop's
        ``call_soon_threadsafe``.
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            if not self._pending:
                return False
            entry = self._pending.popleft()
            if entry.item.deadline_monotonic <= now:
                expired = True
            else:
                expired = False
                self._running = entry
        if expired:
            # Deadline elapsed before the item could start: do not run it.
            self._resolve(entry, exception=QueueItemExpired("deadline exceeded before start"))
            return True
        # Operation runs on this (owner) thread, outside the lock so cancel() /
        # shutdown() from another thread cannot deadlock against it.
        result: object = _UNSET
        op_exception: BaseException | None = None
        try:
            result = entry.operation()
        except Exception as exc:
            # Operation failures must reach the awaiter, never swallowed.
            op_exception = exc
        with self._lock:
            discard = entry.discard
            self._running = None
        if op_exception is not None:
            self._resolve(entry, exception=op_exception)
        elif discard:
            # Cancelled mid-run: the operation finished, but discard the result
            # so a cancelled caller never observes it.
            self._resolve(
                entry,
                exception=QueueItemCancelled("cancelled during run; result discarded"),
            )
        else:
            self._resolve(entry, result=result)
        return True

    def cancel(self, request_id: str) -> bool:
        """Cancel a pending or running request by id.

        A queued item is removed and resolved as cancelled without running. A
        running item cannot be interrupted; instead its result is discarded when
        the operation completes. Returns ``True`` if the id was found. Safe to
        call from any thread; the future is resolved via the owning loop.
        """
        with self._lock:
            if (
                self._running is not None
                and self._running.item.request_id == request_id
            ):
                self._running.discard = True
                return True
            found: _Entry | None = None
            remaining: deque[_Entry] = deque()
            for entry in self._pending:
                if found is None and entry.item.request_id == request_id:
                    found = entry
                    continue
                remaining.append(entry)
            self._pending = remaining
        if found is not None:
            self._resolve(found, exception=QueueItemCancelled("cancelled before start"))
            return True
        return False

    def shutdown(self) -> None:
        """Stop accepting new work and reject all queued items.

        Idempotent. Leaves no pending waiter behind. Safe to call from any
        thread; each drained future is resolved via its owning loop.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            drained = list(self._pending)
            self._pending.clear()
        for entry in drained:
            self._resolve(entry, exception=QueueRejected("queue shut down"))

    @property
    def pending_count(self) -> int:
        """Number of items not yet in a terminal state (queued or running)."""
        with self._lock:
            return len(self._pending) + (1 if self._running is not None else 0)

    # --------------------------------------------------------------- internals

    def _resolve(
        self,
        entry: _Entry,
        *,
        result: object = _UNSET,
        exception: BaseException | None = None,
    ) -> None:
        """Resolve ``entry.future`` on its owning loop, thread-safely.

        The actual ``set_result``/``set_exception`` is scheduled with
        ``call_soon_threadsafe`` on the future's loop (so the transport await
        wakes regardless of which thread calls this) and guarded by
        ``done()`` so an already-resolved future is never set twice. A closed
        loop (transport gone) is swallowed rather than leaking an exception.
        """
        if exception is not None:
            callback, arg = _set_exception_safely, exception
        else:
            callback, arg = _set_result_safely, result
        try:
            entry.loop.call_soon_threadsafe(callback, entry.future, arg)
        except RuntimeError:
            # The owning loop is closed (e.g. transport thread already gone):
            # nothing left to wake; do not leak the error.
            pass

    def _is_pending_locked(self, request_id: str) -> bool:
        """Caller must hold ``self._lock``."""
        if (
            self._running is not None
            and self._running.item.request_id == request_id
        ):
            return True
        return any(entry.item.request_id == request_id for entry in self._pending)
