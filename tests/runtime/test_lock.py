"""Task 11: cross-platform exclusive RuntimeLock tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from eee_agent.core import AgentException
from eee_agent.runtime.lock import RuntimeLock

_LOCK_SCRIPT = (
    "import sys\n"
    "from eee_agent.runtime.lock import RuntimeLock\n"
    "path = sys.argv[1]\n"
    "lock = RuntimeLock(path)\n"
    "lock.__enter__()\n"
    "sys.stdout.write('LOCKED\\n')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.readline()\n"
    "lock.__exit__(None, None, None)\n"
)

_HOLD_SCRIPT = (
    "import sys, time\n"
    "from eee_agent.runtime.lock import RuntimeLock\n"
    "lock = RuntimeLock(sys.argv[1])\n"
    "lock.__enter__()\n"
    "sys.stdout.write('LOCKED\\n')\n"
    "sys.stdout.flush()\n"
    "time.sleep(3600)\n"
)


def _lock_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "runtime.lock"


def _acquire_after_holder_exit(path: Path, timeout: float = 10.0) -> RuntimeLock:
    """Acquire the lock after the holder process exited.

    On Windows, ``proc.wait()`` returns as soon as the child is reaped, but
    the kernel can take a few more milliseconds to tear down the child's file
    handles and release the byte-range lock. Retry contention briefly instead
    of failing on that window.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return RuntimeLock(path).__enter__()
        except AgentException as exc:
            if (
                exc.error.code != "runtime.already_running"
                or time.monotonic() >= deadline
            ):
                raise
            time.sleep(0.05)


# --------------------------------------------------------------------------
# in-process acquire / release
# --------------------------------------------------------------------------


def test_first_lock_acquires(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    with RuntimeLock(path) as lock:
        assert lock is not None
        assert path.exists()
        assert path.stat().st_size >= 1


def test_lock_file_has_at_least_one_byte(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    with RuntimeLock(path):
        pass
    assert path.exists()
    assert path.stat().st_size >= 1


def test_reacquire_after_close(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    with RuntimeLock(path):
        pass
    with RuntimeLock(path):
        pass


def test_exit_releases_under_business_exception(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    with pytest.raises(ValueError, match="boom"):
        with RuntimeLock(path):
            raise ValueError("boom")
    # The lock was released despite the exception; a new acquire succeeds.
    with RuntimeLock(path):
        pass


def test_exit_does_not_swallow_business_exception(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    with pytest.raises(RuntimeError, match="business"):
        with RuntimeLock(path):
            raise RuntimeError("business")


def test_repeat_close_is_safe(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    lock = RuntimeLock(path)
    lock.__enter__()
    lock.close()
    lock.close()  # idempotent, no error
    # and __exit__ after close is also safe
    lock.__exit__(None, None, None)


def test_close_without_enter_is_safe(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    lock = RuntimeLock(path)
    lock.close()  # never acquired; no error


# --------------------------------------------------------------------------
# real cross-process contention (spawned Python process)
# --------------------------------------------------------------------------


def test_second_instance_from_another_process_is_rejected(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_SCRIPT, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCKED"
        with pytest.raises(AgentException) as exc:
            with RuntimeLock(path):
                pass
        assert exc.value.error.code == "runtime.already_running"
    finally:
        assert proc.stdin is not None
        proc.stdin.write("GO\n")
        proc.stdin.flush()
        proc.wait(timeout=15)

    # After the holder exits, the lock is available again.
    with RuntimeLock(path):
        pass


def test_os_releases_lock_on_subprocess_termination(tmp_path: Path) -> None:
    path = _lock_path(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_SCRIPT, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "LOCKED"
        with pytest.raises(AgentException) as exc:
            with RuntimeLock(path):
                pass
        assert exc.value.error.code == "runtime.already_running"
    finally:
        proc.terminate()
        proc.wait(timeout=15)

    # The OS released the lock when the holder process died (no graceful
    # __exit__), so a new acquire succeeds. On Windows the byte-range lock
    # release can lag process reaping by a few milliseconds, so acquire via
    # the bounded-retry helper.
    lock = _acquire_after_holder_exit(path)
    lock.close()


def test_second_in_process_lock_is_rejected(tmp_path: Path) -> None:
    # A second lock object for the same path in the same process is also
    # rejected (the OS byte-range/flock lock is held by the first handle).
    path = _lock_path(tmp_path)
    with RuntimeLock(path):
        with pytest.raises(AgentException) as exc:
            with RuntimeLock(path):
                pass
        assert exc.value.error.code == "runtime.already_running"


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------


def test_non_contention_fs_error_is_not_masqueraded_as_running(
    tmp_path: Path,
) -> None:
    # The lock path's parent is a file, not a directory; mkdir must fail with a
    # filesystem error that is NOT runtime.already_running.
    parent = tmp_path / "afile"
    parent.write_text("x", encoding="utf-8")
    path = parent / "runtime.lock"
    with pytest.raises(OSError):
        RuntimeLock(path).__enter__()


def test_lock_uses_platform_appropriate_api() -> None:
    import eee_agent.runtime.lock as lock_mod

    source = open(lock_mod.__file__, encoding="utf-8").read()
    if os.name == "nt":
        assert "msvcrt" in source
        assert "LK_NBLCK" in source
    else:
        assert "fcntl" in source
        assert "LOCK_EX" in source
        assert "LOCK_NB" in source
