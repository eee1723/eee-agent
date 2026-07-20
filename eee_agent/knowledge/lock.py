"""Exclusive, stale-aware build lock for the knowledge cache.

A lock is acquired with ``O_CREAT|O_EXCL|O_WRONLY`` so two builds cannot hold
the same cache. Ownership (PID, start time, nonce) is stored as JSON. A stale
lock is reclaimed only when its PID no longer exists AND its age exceeds the
explicit TTL; release only removes a lock whose nonce matches, so one build can
never delete another active build's lock.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

__all__ = [
    "LOCK_STALE_TTL_SECONDS",
    "LockBusy",
    "LockError",
    "LockOwner",
    "acquire_build_lock",
    "lock_path_for",
    "release_build_lock",
]

# A lock may be reclaimed only once its owner PID is gone and it is older than
# this constant. Builds inject a smaller value in tests; production uses this.
LOCK_STALE_TTL_SECONDS = 3600


class LockError(Exception):
    """Base class for build-lock failures."""


class LockBusy(LockError):
    """The cache is locked by another (still-active) build."""


@dataclass(frozen=True, slots=True)
class LockOwner:
    pid: int
    started_at: str
    nonce: str


@dataclass(frozen=True, slots=True)
class _OwnerRecord:
    pid: int
    started_at: datetime
    nonce: str


def lock_path_for(cache_path: Path) -> Path:
    """Return the lock path that guards ``cache_path`` (sibling ``.lock`` file)."""
    cache_path = Path(cache_path)
    return cache_path.with_name(cache_path.name + ".lock")


def _serialize(pid: int, started_at: datetime, nonce: str) -> str:
    return json.dumps(
        {"pid": pid, "started_at": started_at.isoformat(), "nonce": nonce},
        sort_keys=True,
        separators=(",", ":"),
    )


def _try_create(lock_path: Path, payload: str) -> bool:
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _read_owner(lock_path: Path) -> _OwnerRecord | None:
    try:
        text = Path(lock_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(text)
        return _OwnerRecord(
            int(data["pid"]),
            datetime.fromisoformat(data["started_at"]),
            str(data["nonce"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def _is_stale(
    owner: _OwnerRecord,
    pid_exists: Callable[[int], bool],
    now: Callable[[], datetime],
    stale_ttl_seconds: int,
) -> bool:
    if pid_exists(owner.pid):
        return False
    return (now() - owner.started_at).total_seconds() >= stale_ttl_seconds


def acquire_build_lock(
    lock_path: Path,
    *,
    pid: int,
    started_at: datetime,
    nonce: str,
    pid_exists: Callable[[int], bool],
    now: Callable[[], datetime],
    stale_ttl_seconds: int = LOCK_STALE_TTL_SECONDS,
) -> LockOwner:
    """Acquire the lock at ``lock_path`` or raise :class:`LockBusy`.

    On success the lock is owned by ``(pid, nonce)``. An existing lock is only
    reclaimed when it is readable, its PID is gone and its age exceeds
    ``stale_ttl_seconds``; an unreadable or fresh lock belonging to a live PID
    is left untouched.
    """
    lock_path = Path(lock_path)
    payload = _serialize(pid, started_at, nonce)
    if _try_create(lock_path, payload):
        return LockOwner(pid, started_at.isoformat(), nonce)

    owner = _read_owner(lock_path)
    if owner is None:
        # Present but unreadable: never reclaim blindly (could be an in-flight
        # write by another build).
        raise LockBusy("build lock is present but unreadable")
    if not _is_stale(owner, pid_exists, now, stale_ttl_seconds):
        raise LockBusy(f"build lock held by pid {owner.pid}")

    # Stale: remove and re-acquire exclusively.
    try:
        os.unlink(str(lock_path))
    except FileNotFoundError:
        pass
    if not _try_create(lock_path, payload):
        raise LockBusy("build lock acquired by another process")
    return LockOwner(pid, started_at.isoformat(), nonce)


def release_build_lock(lock_path: Path, *, nonce: str) -> None:
    """Remove the lock only if it is owned by ``nonce``."""
    lock_path = Path(lock_path)
    owner = _read_owner(lock_path)
    if owner is not None and owner.nonce == nonce:
        try:
            os.unlink(str(lock_path))
        except FileNotFoundError:
            pass
