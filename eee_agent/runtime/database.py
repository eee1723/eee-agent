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
    "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
)


def _validate_history(applied: list[int]) -> None:
    if len(set(applied)) != len(applied):
        raise RuntimeError("duplicate schema migration versions in history")
    if applied and max(applied) > migrations.SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {max(applied)} is newer than this Runtime "
            f"(version {migrations.SCHEMA_VERSION})"
        )
    expected = list(range(1, len(applied) + 1))
    if sorted(applied) != expected:
        raise RuntimeError(
            f"non-contiguous schema migration history: {sorted(applied)}"
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
            await connection.execute(_MIGRATIONS_DDL)
            applied = await cls._read_applied(connection)
            _validate_history(applied)
            for version, script in migrations.MIGRATIONS:
                if version in applied:
                    continue
                await cls._apply_migration(connection, version, script)
        except BaseException:
            await connection.close()
            raise
        return cls(connection)

    @staticmethod
    async def _read_applied(connection: aiosqlite.Connection) -> list[int]:
        cursor = await connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )
        rows = await cursor.fetchall()
        return [int(row[0]) for row in rows]

    @staticmethod
    async def _apply_migration(
        connection: aiosqlite.Connection, version: int, script: str
    ) -> None:
        applied_at = datetime.now(timezone.utc).isoformat()
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
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, applied_at),
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
