"""Crash-safe, content-addressed artifact lifecycle storage.

Metadata transitions are committed independently from filesystem operations.
This prevents SQLite rollback from resurrecting an ``available`` row whose
bytes were already removed and gives startup reconciliation enough information
to finish interrupted placement/eviction work.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import aiosqlite

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.ids import IdKind, require_id
from eee_agent.runtime.database import RuntimeDatabase

_HASH_CHUNK_BYTES = 1024 * 1024
MAX_ARTIFACTS_PER_SESSION = 32
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_ARTIFACTS_GLOBAL = 256
MAX_GLOBAL_BYTES = 512 * 1024 * 1024
_MAX_LIST_LIMIT = 256
_STAGING_DIR = ".staging"
_STATES = "'pending','available','pending_eviction','evicted','missing','failed'"
_COLUMNS = "artifact_id, session_id, run_id, relative_path, sha256, media_type, size_bytes, created_at"


def _artifact_error(code: str, message: str, *, retryable: bool = False) -> AgentException:
    return AgentException(AgentError(code=code, category=ErrorCategory.INTERNAL_INVARIANT, message_for_user=message, retryable=retryable))


def _artifact_missing() -> AgentException:
    return _artifact_error("runtime.artifact_missing", "The captured artifact file is missing or unreadable.")


def _artifact_hash_mismatch() -> AgentException:
    return _artifact_error("runtime.artifact_hash_mismatch", "The captured artifact bytes do not match the bridge-reported digest.")


def _artifact_invalid() -> AgentException:
    return _artifact_error("runtime.artifact_invalid", "The artifact registration is invalid or conflicts with an existing record.")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _row_to_ref(row: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(artifact_id=row["artifact_id"], relative_path=row["relative_path"], sha256=row["sha256"], media_type=row["media_type"], size_bytes=row["size_bytes"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArtifactStore:
    """Registers, resolves, reconciles, and bounds artifact files."""

    def __init__(self, database: RuntimeDatabase, artifacts_root: Path) -> None:
        if not isinstance(database, RuntimeDatabase):
            raise TypeError("database must be a RuntimeDatabase")
        if not isinstance(artifacts_root, Path):
            raise TypeError("artifacts_root must be a Path")
        self._database = database
        self._root = artifacts_root

    @property
    def root(self) -> Path:
        return self._root

    def _canonical(self, relative_path: str) -> Path:
        return self._root.joinpath(*PurePosixPath(relative_path).parts)

    async def register(self, *, session_id: str, run_id: str, artifact_id: str, source: Path, media_type: str, expected_sha256: str, expected_size_bytes: int) -> ArtifactRef:
        require_id(session_id, IdKind.SESSION)
        require_id(run_id, IdKind.RUN)
        require_id(artifact_id, IdKind.ARTIFACT)
        if type(media_type) is not str or not media_type:
            raise ValueError("media_type must be a non-empty string")
        if type(expected_size_bytes) is not int or expected_size_bytes <= 0:
            raise ValueError("expected_size_bytes must be a positive integer")
        if not isinstance(source, Path):
            raise TypeError("source must be a Path")
        try:
            actual_sha256, actual_size = _hash_file(source)
        except OSError:
            raise _artifact_missing() from None
        relative_path = f"{session_id}/{run_id}/{source.name}"
        canonical = self._canonical(relative_path)
        if actual_sha256 != expected_sha256 or actual_size != expected_size_bytes:
            # A caller must not be able to delete an already-available
            # canonical file merely by supplying a stale digest.
            existing_available = await self._database.fetchone(
                "SELECT 1 FROM artifacts WHERE session_id = ? AND relative_path = ? AND artifact_state = 'available'",
                (session_id, relative_path),
            )
            if existing_available is None:
                _unlink_quietly(source)
            raise _artifact_hash_mismatch()

        ref = ArtifactRef(artifact_id=artifact_id, relative_path=relative_path, sha256=actual_sha256, media_type=media_type, size_bytes=actual_size)
        # Reject identity/path collisions before moving bytes. In particular,
        # never replace an existing available canonical file.
        existing = await self._database.fetchone("SELECT artifact_id, artifact_state FROM artifacts WHERE artifact_id = ? OR (session_id = ? AND relative_path = ?)", (artifact_id, session_id, relative_path))
        if existing is not None:
            raise _artifact_invalid()
        if canonical.exists() and source.resolve() != canonical.resolve():
            raise _artifact_invalid()

        self._root.mkdir(parents=True, exist_ok=True)
        staging = self._root / _STAGING_DIR / f"{artifact_id}-{uuid.uuid4().hex}.stage"
        staging.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.resolve() != staging.resolve():
                os.replace(source, staging)
        except OSError:
            raise _artifact_missing() from None
        created_at = _now()
        try:
            async with self._database.write_transaction() as conn:
                await conn.execute("INSERT INTO artifacts(artifact_id,session_id,run_id,relative_path,sha256,media_type,size_bytes,redacted,created_at,schema_version,artifact_state,cleanup_attempts,last_error_code,updated_at) VALUES (?,?,?,?,?,?,?,0,?,1,'pending',0,NULL,?)", (artifact_id, session_id, run_id, relative_path, actual_sha256, media_type, actual_size, created_at, created_at))
        except sqlite3.IntegrityError:
            _unlink_quietly(staging)
            raise _artifact_invalid() from None

        try:
            canonical.parent.mkdir(parents=True, exist_ok=True)
            if canonical.exists():
                raise FileExistsError(str(canonical))
            os.replace(staging, canonical)
            placed_hash, placed_size = _hash_file(canonical)
            if placed_hash != actual_sha256 or placed_size != actual_size:
                raise ValueError("canonical bytes changed during placement")
            await self._mark_available(artifact_id)
        except BaseException as exc:
            await self._mark_failed(artifact_id, "placement_failed", retryable=True)
            if isinstance(exc, (AgentException, RuntimeError)):
                raise
            raise _artifact_missing() from None
        await self._enforce_retention(session_id, keep_id=artifact_id)
        return ref

    async def _mark_available(self, artifact_id: str) -> None:
        async with self._database.write_transaction() as conn:
            await conn.execute("UPDATE artifacts SET artifact_state='available', updated_at=?, last_error_code=NULL WHERE artifact_id=? AND artifact_state='pending'", (_now(), artifact_id))

    async def _mark_failed(self, artifact_id: str, code: str, *, retryable: bool) -> None:
        async with self._database.write_transaction() as conn:
            await conn.execute("UPDATE artifacts SET artifact_state='failed', updated_at=?, last_error_code=? WHERE artifact_id=? AND artifact_state IN ('pending','available')", (_now(), code, artifact_id))

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        require_id(artifact_id, IdKind.ARTIFACT)
        row = await self._database.fetchone(f"SELECT {_COLUMNS} FROM artifacts WHERE artifact_id = ? AND artifact_state = 'available'", (artifact_id,))
        return None if row is None else _row_to_ref(row)

    async def list_for_session(self, session_id: str, *, limit: int = 64) -> tuple[ArtifactRef, ...]:
        require_id(session_id, IdKind.SESSION)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError("limit must be in 1..256")
        rows = await self._database.fetchall(f"SELECT {_COLUMNS} FROM artifacts WHERE session_id = ? AND artifact_state = 'available' ORDER BY created_at DESC, artifact_id DESC LIMIT ?", (session_id, limit))
        return tuple(_row_to_ref(row) for row in rows)

    async def list_for_run(self, session_id: str, run_id: str, *, limit: int = 64) -> tuple[ArtifactRef, ...]:
        require_id(session_id, IdKind.SESSION)
        require_id(run_id, IdKind.RUN)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError("limit must be in 1..256")
        rows = await self._database.fetchall(f"SELECT {_COLUMNS} FROM artifacts WHERE session_id = ? AND run_id = ? AND artifact_state = 'available' ORDER BY created_at DESC, artifact_id DESC LIMIT ?", (session_id, run_id, limit))
        return tuple(_row_to_ref(row) for row in rows)

    def path_for(self, ref: ArtifactRef) -> Path:
        if type(ref) is not ArtifactRef:
            raise TypeError("ref must be an exact ArtifactRef")
        return self._canonical(ref.relative_path)

    async def delete_session_artifacts(self, session_id: str) -> int:
        require_id(session_id, IdKind.SESSION)
        rows = await self._database.fetchall("SELECT artifact_id, relative_path FROM artifacts WHERE session_id = ? AND artifact_state IN ('available','pending','pending_eviction','failed','missing')", (session_id,))
        if not rows:
            return 0
        async with self._database.write_transaction() as conn:
            await conn.execute("UPDATE artifacts SET artifact_state='pending_eviction', updated_at=? WHERE session_id=? AND artifact_state IN ('available','pending','failed','missing','pending_eviction')", (_now(), session_id))
        try:
            session_dir = self._root / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir)
        except OSError as exc:
            await self._record_cleanup_failure(session_id=session_id, code="session_delete_failed")
            raise exc
        async with self._database.write_transaction() as conn:
            await conn.execute("UPDATE artifacts SET artifact_state='evicted', updated_at=?, last_error_code=NULL WHERE session_id=? AND artifact_state='pending_eviction'", (_now(), session_id))
        return len(rows)

    async def _record_cleanup_failure(self, *, session_id: str | None = None, artifact_id: str | None = None, code: str) -> None:
        where = "session_id = ?" if session_id is not None else "artifact_id = ?"
        value = session_id if session_id is not None else artifact_id
        async with self._database.write_transaction() as conn:
            await conn.execute(f"UPDATE artifacts SET cleanup_attempts=cleanup_attempts+1, last_error_code=?, updated_at=? WHERE {where} AND artifact_state='pending_eviction'", (code, _now(), value))

    async def _enforce_retention(self, session_id: str, *, keep_id: str) -> None:
        await self._evict_over_caps(where="session_id = ?", params=(session_id,), max_count=MAX_ARTIFACTS_PER_SESSION, max_bytes=MAX_SESSION_BYTES, keep_id=keep_id)
        await self._evict_over_caps(where="1 = 1", params=(), max_count=MAX_ARTIFACTS_GLOBAL, max_bytes=MAX_GLOBAL_BYTES, keep_id=keep_id)

    async def _evict_over_caps(self, *, where: str, params: tuple[object, ...], max_count: int, max_bytes: int, keep_id: str) -> None:
        rows = await self._database.fetchall(f"SELECT artifact_id, relative_path, size_bytes FROM artifacts WHERE {where} AND artifact_state='available' AND artifact_id != ? ORDER BY created_at ASC, artifact_id ASC", (*params, keep_id))
        totals = await self._database.fetchone(f"SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes),0) AS b FROM artifacts WHERE {where} AND artifact_state='available'", params)
        count = int(totals["c"])
        total_bytes = int(totals["b"])
        candidates: list[sqlite3.Row] = []
        for row in rows:
            if count - len(candidates) <= max_count and total_bytes <= max_bytes:
                break
            candidates.append(row)
            total_bytes -= int(row["size_bytes"])
        if not candidates:
            return
        ids = [row["artifact_id"] for row in candidates]
        async with self._database.write_transaction() as conn:
            for artifact_id in ids:
                await conn.execute("UPDATE artifacts SET artifact_state='pending_eviction', updated_at=? WHERE artifact_id=? AND artifact_state='available'", (_now(), artifact_id))
        for row in candidates:
            path = self._canonical(row["relative_path"])
            try:
                path.unlink(missing_ok=True)
            except OSError:
                await self._record_cleanup_failure(artifact_id=row["artifact_id"], code="retention_delete_failed")
                continue
            async with self._database.write_transaction() as conn:
                await conn.execute("UPDATE artifacts SET artifact_state='evicted', updated_at=?, last_error_code=NULL WHERE artifact_id=? AND artifact_state='pending_eviction'", (_now(), row["artifact_id"]))

    async def reconcile(self) -> None:
        rows = await self._database.fetchall("SELECT artifact_id, relative_path, sha256, size_bytes, artifact_state FROM artifacts WHERE artifact_state IN ('pending','pending_eviction','available')")
        for row in rows:
            artifact_id = row["artifact_id"]
            canonical = self._canonical(row["relative_path"])
            if row["artifact_state"] == "pending_eviction":
                try:
                    canonical.unlink(missing_ok=True)
                except OSError:
                    await self._record_cleanup_failure(artifact_id=artifact_id, code="retention_delete_failed")
                else:
                    async with self._database.write_transaction() as conn:
                        await conn.execute("UPDATE artifacts SET artifact_state='evicted', updated_at=?, last_error_code=NULL WHERE artifact_id=?", (_now(), artifact_id))
                continue
            if row["artifact_state"] == "pending":
                stages = sorted((self._root / _STAGING_DIR).glob(f"{artifact_id}-*.stage"))
                if not canonical.exists() and stages:
                    canonical.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(stages[0], canonical)
                    except OSError:
                        pass
                if canonical.exists():
                    try:
                        digest, size = _hash_file(canonical)
                    except OSError:
                        digest, size = "", -1
                    if digest == row["sha256"] and size == int(row["size_bytes"]):
                        await self._mark_available(artifact_id)
                        continue
                await self._mark_failed(artifact_id, "pending_recovery_failed", retryable=True)
            elif row["artifact_state"] == "available" and not canonical.is_file():
                async with self._database.write_transaction() as conn:
                    await conn.execute("UPDATE artifacts SET artifact_state='missing', updated_at=?, last_error_code='missing_file' WHERE artifact_id=?", (_now(), artifact_id))
        staging = self._root / _STAGING_DIR
        if staging.is_dir():
            for path in staging.glob("*.stage"):
                _unlink_quietly(path)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["ArtifactStore", "MAX_ARTIFACTS_GLOBAL", "MAX_ARTIFACTS_PER_SESSION", "MAX_GLOBAL_BYTES", "MAX_SESSION_BYTES"]
