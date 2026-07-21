"""Cross-platform exclusive Runtime-home lock.

Holds an OS-level exclusive lock on a one-byte lock file so only one Runtime
process owns a given Runtime home. Windows uses ``msvcrt.locking`` (LK_NBLCK);
POSIX uses ``fcntl.flock`` (LOCK_EX | LOCK_NB). Genuine lock contention is
converted to ``runtime.already_running``; any other filesystem error is
propagated unchanged. The OS releases the lock if the process dies.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

from eee_agent.core import AgentError, AgentException, ErrorCategory

# errno values that indicate "another process holds the lock".
_CONTENTION_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EDEADLK,
        errno.EDEADLOCK,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
    }
)


def _already_running() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.already_running",
            category=ErrorCategory.VALIDATION,
            message_for_user="A Runtime is already running for this home.",
        )
    )


def _is_contention(exc: OSError) -> bool:
    return exc.errno in _CONTENTION_ERRNOS


class RuntimeLock:
    """An exclusive OS lock on a lock file, usable as a context manager."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._fh: object | None = None
        self._acquired = False

    @property
    def path(self) -> Path:
        return self._path

    def __enter__(self) -> "RuntimeLock":
        if self._acquired:
            raise RuntimeError("RuntimeLock is already held")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open/create the lock file read/write, binary. "a+b" creates it if
        # absent without truncating an existing file.
        fh = open(self._path, "a+b")
        try:
            self._acquire(fh)
        except BaseException:
            try:
                fh.close()
            except OSError:
                pass
            raise
        self._fh = fh
        self._acquired = True
        return self

    def _acquire(self, fh) -> None:
        # Ensure the file has at least one byte (msvcrt.locking locks a byte
        # range; a zero-byte file cannot be locked).
        fh.seek(0, os.SEEK_END)
        if fh.tell() == 0:
            fh.write(b"\x00")
            fh.flush()
            os.fsync(fh.fileno())
        # Position at byte 0 before locking.
        fh.seek(0)
        try:
            if os.name == "nt":
                self._lock_windows(fh)
            else:
                self._lock_posix(fh)
        except OSError as exc:
            if _is_contention(exc):
                raise _already_running() from exc
            raise

    @staticmethod
    def _lock_windows(fh) -> None:
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    @staticmethod
    def _lock_posix(fh) -> None:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc, tb) -> None:
        # Always release; never suppress a business exception (return None).
        self.close()

    def close(self) -> None:
        """Release the lock and close the file handle. Idempotent."""
        fh = self._fh
        self._fh = None
        self._acquired = False
        if fh is None:
            return
        try:
            fh.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                # Closing the handle releases the OS lock regardless.
                pass
        finally:
            try:
                fh.close()
            except OSError:
                pass
