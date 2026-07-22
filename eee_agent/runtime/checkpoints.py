from __future__ import annotations

from contextlib import AbstractAsyncContextManager
import os
from pathlib import Path
from types import TracebackType

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


class CheckpointManager:
    """Owns the lifecycle of one LangGraph ``AsyncSqliteSaver``.

    The checkpoint database is independent from the Runtime application
    database. Consumers must use :meth:`require_saver` so a closed manager
    never exposes a ``None`` saver to the graph.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._context: AbstractAsyncContextManager[AsyncSqliteSaver] | None = None
        self.saver: AsyncSqliteSaver | None = None

    def require_saver(self) -> AsyncSqliteSaver:
        saver = self.saver
        if saver is None:
            raise RuntimeError("checkpoint manager is not open")
        return saver

    async def __aenter__(self) -> CheckpointManager:
        if self._context is not None or self.saver is not None:
            raise RuntimeError("checkpoint manager is already open")

        # Enable strict msgpack before the saver is constructed/entered. Use
        # setdefault so an explicit caller value is never overridden.
        os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
        context = AsyncSqliteSaver.from_conn_string(str(self._path))
        self._context = context
        try:
            saver = await context.__aenter__()
            self.saver = saver
        except BaseException:
            # Context enter failed: nothing entered to exit, just reset state.
            self._context = None
            raise
        try:
            await saver.setup()
        except BaseException:
            # setup failed after a successful enter: exit the entered context,
            # reset public state, and propagate the original setup error.
            self.saver = None
            self._context = None
            try:
                await context.__aexit__(None, None, None)
            except BaseException:
                pass
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        context = self._context
        # Reset public state first so the manager never looks open, even if the
        # context exit itself raises. Returning None (falsy) never suppresses
        # the caller's exception.
        self.saver = None
        self._context = None
        if context is not None:
            try:
                await context.__aexit__(exc_type, exc, traceback)
            except BaseException:
                # A cleanup error must not mask an existing business exception.
                if exc is None:
                    raise

    async def delete_thread(self, session_id: str) -> None:
        await self.require_saver().adelete_thread(session_id)
