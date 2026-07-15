from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from eee_agent.runtime import migrations

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

_MIGRATIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, "
    "checksum TEXT NOT NULL)"
)


def _validate_history(applied: list[tuple[int, str]]) -> None:
    versions = [version for version, _ in applied]
    if len(set(versions)) != len(versions):
        raise RuntimeError("duplicate schema migration versions in history")
    if versions and max(versions) > migrations.SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {max(versions)} is newer than this Runtime "
            f"(version {migrations.SCHEMA_VERSION})"
        )
    expected = list(range(1, len(versions) + 1))
    if sorted(versions) != expected:
        raise RuntimeError(
            f"non-contiguous schema migration history: {sorted(versions)}"
        )
    # Every applied row must carry the deterministic SHA-256 of its exact SQL
    # script. A tampered checksum (or a script changed without a version bump)
    # fails closed before any migration runs.
    for version, stored_checksum in applied:
        script = migrations.script_for_version(version)
        if script is None:
            raise RuntimeError(
                f"no migration script for applied schema version {version}"
            )
        if stored_checksum != migrations.migration_checksum(script):
            raise RuntimeError(
                f"checksum mismatch for schema migration version {version}"
            )


class RuntimeDatabase:
    """Owns one long-lived aiosqlite connection and one asyncio write lock.

    The connection runs in autocommit (``isolation_level=None``) so that
    explicit ``BEGIN IMMEDIATE``/``COMMIT``/``ROLLBACK`` give full manual
    transaction control; this keeps migration DDL atomic and lets
    ``write_transaction`` serialize transaction bodies without SQLite's
    implicit-BEGIN interfering.
    """

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._write_lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: str | Path) -> "RuntimeDatabase":
        connection = await aiosqlite.connect(str(path), isolation_level=None)
        try:
            connection.row_factory = aiosqlite.Row
            for pragma in _PRAGMAS:
                await connection.execute(pragma)
            await cls._ensure_schema_migrations_table(connection)
            applied = await cls._read_applied(connection)
            _validate_history(applied)
            applied_versions = {version for version, _ in applied}
            for version, script in migrations.MIGRATIONS:
                if version in applied_versions:
                    continue
                await cls._apply_migration(connection, version, script)
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    @staticmethod
    async def _ensure_schema_migrations_table(
        connection: aiosqlite.Connection,
    ) -> None:
        """Create the migrations table, and upgrade a legacy v1 table.

        A fresh database gets the checksum column from :data:`_MIGRATIONS_DDL`.
        A legacy v1 database whose ``schema_migrations`` table predates
        checksums is upgraded transactionally: the column is added and every
        already-applied version is backfilled with the deterministic checksum of
        its known script. Unknown/future versions are left for
        :func:`_validate_history` to reject.
        """
        await connection.execute(_MIGRATIONS_DDL)
        cursor = await connection.execute("PRAGMA table_info(schema_migrations)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "checksum" in columns:
            return
        await connection.execute("BEGIN IMMEDIATE")
        try:
            await connection.execute(
                "ALTER TABLE schema_migrations "
                "ADD COLUMN checksum TEXT NOT NULL DEFAULT ''"
            )
            for version, script in migrations.MIGRATIONS:
                await connection.execute(
                    "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                    (migrations.migration_checksum(script), version),
                )
            await connection.execute("COMMIT")
        except BaseException:
            try:
                await connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    async def _read_applied(connection: aiosqlite.Connection) -> list[tuple[int, str]]:
        cursor = await connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        )
        rows = await cursor.fetchall()
        return [(int(row[0]), row[1]) for row in rows]

    @staticmethod
    async def _apply_migration(
        connection: aiosqlite.Connection, version: int, script: str
    ) -> None:
        applied_at = datetime.now(timezone.utc).isoformat()
        checksum = migrations.migration_checksum(script)
        await connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in migrations.split_sql_statements(script):
                await connection.execute(statement)
            if version == 1:
                # The runtime_state singleton row is part of the v1 schema and
                # is seeded in the same atomic transaction as the DDL.
                await connection.execute(
                    "INSERT INTO runtime_state(singleton_id, active_run_id, updated_at) "
                    "VALUES (1, NULL, ?)",
                    (applied_at,),
                )
            await connection.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (?, ?, ?)",
                (version, applied_at, checksum),
            )
            await connection.execute("COMMIT")
        except BaseException:
            try:
                await connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    async def close(self) -> None:
        await self._connection.close()

    async def schema_version(self) -> int:
        row = await self.fetchone("SELECT MAX(version) AS version FROM schema_migrations")
        if row is None or row["version"] is None:
            return 0
        return int(row["version"])

    async def table_names(self) -> set[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        return {row["name"] for row in rows}

    async def fetchone(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        cursor = await self._connection.execute(sql, parameters)
        return await cursor.fetchone()

    async def fetchall(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await self._connection.execute(sql, parameters)
        return list(await cursor.fetchall())

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        await self._write_lock.acquire()
        try:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                await self._connection.execute("COMMIT")
            except BaseException:
                # Roll back on any failure between BEGIN and the finalized
                # COMMIT, including a deferred-FK IntegrityError raised by the
                # COMMIT itself. Guard on ``in_transaction`` and swallow a
                # rollback-time sqlite error so a secondary failure cannot mask
                # the original exception, which is always re-raised below.
                if self._connection.in_transaction:
                    try:
                        await self._connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise
        finally:
            self._write_lock.release()
