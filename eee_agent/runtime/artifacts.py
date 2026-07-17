"""Content-addressed artifact metadata store with bounded retention (Task 19-A).

The store is the Runtime-side authority for captured artifact bytes. One
stored file is the single source of truth: its SHA-256 is computed once at
capture (Houdini side), independently re-hashed here BEFORE registration, and
stored in the :class:`~eee_agent.core.artifacts.ArtifactRef`; the panel viewer
and any later vision input read the exact same bytes from
:meth:`ArtifactStore.path_for`. A hash/size mismatch fails closed — the file
is deleted and never registered — so a capture failure can never masquerade
as visual success.

Metadata lives in the ``artifacts`` SQLite table (schema v4) so records
survive restart and artifacts stay discoverable after ``RuntimeService.open``;
the ``redacted`` column carries the 19-B redaction-hook shape from day one.
Bounded retention (per-session and global count/byte caps) evicts oldest
first, deterministically, removing both the file and the DB row; eviction
never touches the event log, so replay is unaffected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import aiosqlite

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.core.ids import IdKind, require_id
from eee_agent.runtime.database import RuntimeDatabase

_HASH_CHUNK_BYTES = 1024 * 1024

# Bounded retention caps (deterministic oldest-first eviction).
MAX_ARTIFACTS_PER_SESSION = 32
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_ARTIFACTS_GLOBAL = 256
MAX_GLOBAL_BYTES = 512 * 1024 * 1024

_MAX_LIST_LIMIT = 256

_COLUMNS = "artifact_id, session_id, run_id, relative_path, sha256, media_type, size_bytes, created_at"


def _artifact_error(code: str, message: str, *, retryable: bool = False) -> AgentException:
    return AgentException(
        AgentError(
            code=code,
            category=ErrorCategory.INTERNAL_INVARIANT,
            message_for_user=message,
            retryable=retryable,
        )
    )


def _artifact_missing() -> AgentException:
    return _artifact_error(
        "runtime.artifact_missing",
        "The captured artifact file is missing or unreadable.",
    )


def _artifact_hash_mismatch() -> AgentException:
    return _artifact_error(
        "runtime.artifact_hash_mismatch",
        "The captured artifact bytes do not match the bridge-reported digest.",
    )


def _artifact_invalid() -> AgentException:
    return _artifact_error(
        "runtime.artifact_invalid",
        "The artifact registration is invalid or conflicts with an existing record.",
    )


def _hash_file(path: Path) -> tuple[str, int]:
    """Stream one file's SHA-256 and exact size in bounded chunks."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _row_to_ref(row: sqlite3.Row) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=row["artifact_id"],
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
    )


class ArtifactStore:
    """Registers, resolves, and bounds content-addressed artifact files."""

    def __init__(self, database: RuntimeDatabase, artifacts_root: Path) -> None:
        if not isinstance(database, RuntimeDatabase):
            raise TypeError("database must be a RuntimeDatabase")
        if not isinstance(artifacts_root, Path):
            raise TypeError("artifacts_root must be a Path")
        self._database = database
        self._root = artifacts_root

    @property
    def root(self) -> Path:
        """The absolute artifacts root directory."""
        return self._root

    # ------------------------------------------------------------------
    # registration (verify -> place -> record -> bound)
    # ------------------------------------------------------------------

    async def register(
        self,
        *,
        session_id: str,
        run_id: str,
        artifact_id: str,
        source: Path,
        media_type: str,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> ArtifactRef:
        """Register one captured file as a content-addressed artifact.

        Re-hashes ``source`` and fails closed (deleting the file) when the
        bytes do not match the bridge-returned digest/size. The file is moved
        into the canonical ``<session>/<run>/<name>`` location with an atomic
        rename when needed, the metadata row is inserted, and bounded
        retention is enforced in the same write transaction.
        """
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
        if actual_sha256 != expected_sha256 or actual_size != expected_size_bytes:
            # Fail closed: the delivered bytes are not the reported bytes, so
            # the file is deleted and never registered (never a guessed match).
            _unlink_quietly(source)
            raise _artifact_hash_mismatch()
        relative_path = f"{session_id}/{run_id}/{source.name}"
        canonical = self._root / session_id / run_id / source.name
        # ArtifactRef construction validates the canonical relative path.
        ref = ArtifactRef(
            artifact_id=artifact_id,
            relative_path=relative_path,
            sha256=actual_sha256,
            media_type=media_type,
            size_bytes=actual_size,
        )
        try:
            if source.resolve() != canonical.resolve():
                canonical.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, canonical)
        except OSError:
            _unlink_quietly(source)
            raise _artifact_missing() from None
        created_at = datetime.now(timezone.utc).isoformat()
        async with self._database.write_transaction() as conn:
            try:
                await conn.execute(
                    "INSERT INTO artifacts(artifact_id, session_id, run_id, "
                    "relative_path, sha256, media_type, size_bytes, redacted, "
                    "created_at, schema_version) VALUES (?,?,?,?,?,?,?,0,?,1)",
                    (
                        artifact_id,
                        session_id,
                        run_id,
                        relative_path,
                        actual_sha256,
                        media_type,
                        actual_size,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError:
                raise _artifact_invalid() from None
            await self._enforce_retention(conn, session_id, keep_id=artifact_id)
        return ref

    # ------------------------------------------------------------------
    # lookup / listing / resolution
    # ------------------------------------------------------------------

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        require_id(artifact_id, IdKind.ARTIFACT)
        row = await self._database.fetchone(
            f"SELECT {_COLUMNS} FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )
        return None if row is None else _row_to_ref(row)

    async def list_for_session(
        self, session_id: str, *, limit: int = 64
    ) -> tuple[ArtifactRef, ...]:
        require_id(session_id, IdKind.SESSION)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError("limit must be in 1..256")
        rows = await self._database.fetchall(
            f"SELECT {_COLUMNS} FROM artifacts WHERE session_id = ? "
            "ORDER BY created_at DESC, artifact_id DESC LIMIT ?",
            (session_id, limit),
        )
        return tuple(_row_to_ref(row) for row in rows)

    async def list_for_run(
        self, session_id: str, run_id: str, *, limit: int = 64
    ) -> tuple[ArtifactRef, ...]:
        require_id(session_id, IdKind.SESSION)
        require_id(run_id, IdKind.RUN)
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError("limit must be in 1..256")
        rows = await self._database.fetchall(
            f"SELECT {_COLUMNS} FROM artifacts WHERE session_id = ? AND run_id = ? "
            "ORDER BY created_at DESC, artifact_id DESC LIMIT ?",
            (session_id, run_id, limit),
        )
        return tuple(_row_to_ref(row) for row in rows)

    def path_for(self, ref: ArtifactRef) -> Path:
        """The absolute path of the exact bytes ``ref`` addresses.

        ``ArtifactRef`` construction already proved the relative path is safe
        (POSIX-normalized, no traversal, no unsafe components), so the join
        cannot escape the artifacts root.
        """
        if type(ref) is not ArtifactRef:
            raise TypeError("ref must be an exact ArtifactRef")
        return self._root.joinpath(*PurePosixPath(ref.relative_path).parts)

    # ------------------------------------------------------------------
    # session cleanup (mirrors the checkpoint-thread cleanup seam)
    # ------------------------------------------------------------------

    async def delete_session_artifacts(self, session_id: str) -> int:
        """Remove one session's artifact files and rows; return rows removed."""
        require_id(session_id, IdKind.SESSION)
        # Files first: a removal failure keeps the rows so a later call can
        # retry; rows are deleted only after the directory is gone.
        session_dir = self._root / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM artifacts WHERE session_id = ?", (session_id,)
            )
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # bounded retention (deterministic oldest-first eviction)
    # ------------------------------------------------------------------

    async def _enforce_retention(self, conn: aiosqlite.Connection, session_id: str, *, keep_id: str) -> None:
        await self._evict_over_caps(
            conn,
            where="session_id = ?",
            params=(session_id,),
            max_count=MAX_ARTIFACTS_PER_SESSION,
            max_bytes=MAX_SESSION_BYTES,
            keep_id=keep_id,
        )
        await self._evict_over_caps(
            conn,
            where="1 = 1",
            params=(),
            max_count=MAX_ARTIFACTS_GLOBAL,
            max_bytes=MAX_GLOBAL_BYTES,
            keep_id=keep_id,
        )

    async def _evict_over_caps(
        self,
        conn: aiosqlite.Connection,
        *,
        where: str,
        params: tuple[object, ...],
        max_count: int,
        max_bytes: int,
        keep_id: str,
    ) -> None:
        # Oldest-first deterministic order; the freshly-registered artifact is
        # never an eviction candidate (a lone oversized artifact stays bounded
        # by exactly its own size).
        cursor = await conn.execute(
            "SELECT artifact_id, relative_path, size_bytes FROM artifacts "
            f"WHERE {where} AND artifact_id != ? "
            "ORDER BY created_at ASC, artifact_id ASC",
            (*params, keep_id),
        )
        rows = await cursor.fetchall()
        count_cursor = await conn.execute(
            f"SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS b FROM artifacts WHERE {where}",
            params,
        )
        totals = await count_cursor.fetchone()
        count = int(totals["c"])
        total_bytes = int(totals["b"])
        evicted = 0
        for row in rows:
            if count - evicted <= max_count and total_bytes <= max_bytes:
                break
            _unlink_quietly(self._root.joinpath(*PurePosixPath(row["relative_path"]).parts))
            await conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = ?", (row["artifact_id"],)
            )
            evicted += 1
            total_bytes -= int(row["size_bytes"])


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "ArtifactStore",
    "MAX_ARTIFACTS_GLOBAL",
    "MAX_ARTIFACTS_PER_SESSION",
    "MAX_GLOBAL_BYTES",
    "MAX_SESSION_BYTES",
]
