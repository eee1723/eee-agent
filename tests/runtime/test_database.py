from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest

from eee_agent.runtime import migrations as migrations_mod
from eee_agent.runtime.database import RuntimeDatabase

SESSION_ID = f"ses_{'0' * 32}"
RUN_ID = f"run_{'0' * 32}"
EVENT_ID = f"evt_{'0' * 32}"
NOW_ISO = "2026-07-14T02:30:00+00:00"
VALID_RUN_STATUSES = [
    "Created",
    "PreparingContext",
    "Planning",
    "Finalizing",
    "Completed",
    "StopRequested",
    "Stopping",
    "Cancelled",
    "Retrying",
    "Failed",
]

_SESSION_INSERT = (
    "INSERT INTO sessions(session_id, title, status, created_at, updated_at, "
    "last_seq, replay_floor_seq) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_RUN_INSERT = (
    "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
    "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_EVENT_INSERT = (
    "INSERT INTO events(event_id, session_id, run_id, seq, event_type, timestamp, "
    "payload_json, retention_class, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _run(coro):
    return asyncio.run(coro)


async def add_session(
    db: RuntimeDatabase,
    sid: str = SESSION_ID,
    status: str = "active",
    last_seq: int = 0,
    floor: int = 0,
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _SESSION_INSERT, (sid, "T", status, NOW_ISO, NOW_ISO, last_seq, floor)
        )


async def add_run(
    db: RuntimeDatabase,
    rid: str = RUN_ID,
    sid: str = SESSION_ID,
    status: str = "Created",
    model: str = "{}",
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _RUN_INSERT,
            (rid, sid, status, "in", None, NOW_ISO, None, None, None, model),
        )


async def add_event(
    db: RuntimeDatabase,
    eid: str = EVENT_ID,
    sid: str = SESSION_ID,
    rid: str | None = None,
    seq: int = 1,
    retention: str = "durable",
    schema: int = 1,
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _EVENT_INSERT,
            (eid, sid, rid, seq, "run.test", NOW_ISO, "{}", retention, schema),
        )


# --------------------------------------------------------------------------
# 1. empty database -> schema v4 (v1-v3 tables preserved, artifact metadata added)
# --------------------------------------------------------------------------

def test_empty_database_reaches_schema_v4(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            assert await db.schema_version() == 4
            names = await db.table_names()
            assert {
                "schema_migrations",
                "sessions",
                "runs",
                "events",
                "runtime_state",
                "workspaces",
                "changesets",
                "approvals",
                "change_receipts",
                "session_workspace_state",
                "artifacts",
            } <= names
            row = await db.fetchone(
                "SELECT singleton_id, active_run_id, updated_at FROM runtime_state"
            )
            assert row["singleton_id"] == 1
            assert row["active_run_id"] is None
            ts = datetime.fromisoformat(row["updated_at"])
            assert ts.utcoffset().total_seconds() == 0
            count = await db.fetchone("SELECT COUNT(*) AS c FROM runtime_state")
            assert count["c"] == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 2. re-open idempotence
# --------------------------------------------------------------------------

def test_reopen_does_not_rerun_migration(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        await db.close()
        db2 = await RuntimeDatabase.open(db_path)
        try:
            assert await db2.schema_version() == 4
            rows = await db2.fetchall(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            assert [r["version"] for r in rows] == [1, 2, 3, 4]
        finally:
            await db2.close()

    _run(scenario())


def test_reopen_preserves_application_data(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        await add_session(db)
        await db.close()
        db2 = await RuntimeDatabase.open(db_path)
        try:
            row = await db2.fetchone("SELECT COUNT(*) AS c FROM sessions")
            assert row["c"] == 1
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 3. newer schema rejected + connection cleaned up
# --------------------------------------------------------------------------

def _seed_raw_migrations(db_path: Path, create_sql: str, versions: list[int]) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(create_sql)
        for v in versions:
            raw.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (v, NOW_ISO),
            )
        raw.commit()
    finally:
        raw.close()


def test_newer_schema_is_rejected_and_connection_closed(db_path: Path) -> None:
    _seed_raw_migrations(
        db_path,
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        [5],
    )
    with pytest.raises(RuntimeError, match="newer than this Runtime"):
        _run(RuntimeDatabase.open(db_path))

    # The failed open() must have released the file handle (Windows-safe).
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("SELECT version FROM schema_migrations").fetchone()[0] == 5
    finally:
        raw.close()


# --------------------------------------------------------------------------
# 4. migration history validation
# --------------------------------------------------------------------------

def test_duplicate_migration_history_is_rejected(db_path: Path) -> None:
    _seed_raw_migrations(
        db_path,
        "CREATE TABLE schema_migrations (version INTEGER, applied_at TEXT NOT NULL)",
        [1, 1],
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        _run(RuntimeDatabase.open(db_path))


def test_non_contiguous_history_is_rejected(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations_mod, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(
        migrations_mod,
        "MIGRATIONS",
        ((1, "SELECT 1;"), (2, "SELECT 1;"), (3, "SELECT 1;")),
    )
    _seed_raw_migrations(
        db_path,
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        [1, 3],
    )
    with pytest.raises(RuntimeError, match="non-contiguous"):
        _run(RuntimeDatabase.open(db_path))


def test_history_version_above_schema_version_is_rejected(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migrations_mod, "SCHEMA_VERSION", 3)
    monkeypatch.setattr(
        migrations_mod,
        "MIGRATIONS",
        ((1, "SELECT 1;"), (2, "SELECT 1;"), (3, "SELECT 1;")),
    )
    _seed_raw_migrations(
        db_path,
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
        [5],
    )
    with pytest.raises(RuntimeError, match="newer than this Runtime"):
        _run(RuntimeDatabase.open(db_path))


# --------------------------------------------------------------------------
# 5. migration atomic rollback
# --------------------------------------------------------------------------

def test_failed_migration_rolls_back_without_residue(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # First statement succeeds; second must fail so we can prove the first is
    # rolled back (this would catch an executescript-after-BEGIN residue bug).
    monkeypatch.setattr(
        migrations_mod,
        "MIGRATIONS",
        (
            (
                1,
                "CREATE TABLE tmp_partial(x INTEGER);\n"
                "INSERT INTO no_such_table(x) VALUES (1);",
            ),
        ),
    )
    with pytest.raises(sqlite3.Error):
        _run(RuntimeDatabase.open(db_path))

    raw = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        versions = [
            r[0] for r in raw.execute("SELECT version FROM schema_migrations").fetchall()
        ]
    finally:
        raw.close()
    assert "tmp_partial" not in tables
    assert versions == []


# --------------------------------------------------------------------------
# 6. PRAGMAs
# --------------------------------------------------------------------------

def test_connection_pragmas_are_active(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            assert (await db.fetchone("PRAGMA journal_mode"))[0].lower() == "wal"
            assert (await db.fetchone("PRAGMA foreign_keys"))[0] == 1
            assert (await db.fetchone("PRAGMA busy_timeout"))[0] == 5000
            assert (await db.fetchone("PRAGMA synchronous"))[0] == 1  # NORMAL
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 7. query API
# --------------------------------------------------------------------------

def test_query_api_signatures_and_return_types(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            one = await db.fetchone("SELECT 1 AS v")
            assert isinstance(one, aiosqlite.Row)
            assert one["v"] == 1
            assert await db.fetchone("SELECT 1 AS v WHERE 0") is None
            rows = await db.fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            assert isinstance(rows, list)
            assert rows and all(isinstance(r, aiosqlite.Row) for r in rows)
            assert (await db.fetchone("SELECT ? AS v", (42,)))["v"] == 42
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 8/9/10. write_transaction commit / rollback / BaseException / cancellation
# --------------------------------------------------------------------------

def test_write_transaction_commits_on_success(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_write_transaction_rolls_back_on_exception_and_propagates(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(ValueError):
                async with db.write_transaction() as conn:
                    await conn.execute(_SESSION_INSERT, (SESSION_ID, "T", "active", NOW_ISO, NOW_ISO, 0, 0))
                    raise ValueError("nope")
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


class _BaseBoom(BaseException):
    pass


def test_write_transaction_rolls_back_on_base_exception(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(_BaseBoom):
                async with db.write_transaction() as conn:
                    await conn.execute(_SESSION_INSERT, (SESSION_ID, "T", "active", NOW_ISO, NOW_ISO, 0, 0))
                    raise _BaseBoom("boom")
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 0
            # lock released: a subsequent transaction still works
            await add_session(db)
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_write_transaction_rolls_back_on_cancellation(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            started = asyncio.Event()

            async def slow_write() -> None:
                async with db.write_transaction() as conn:
                    await conn.execute(
                        _SESSION_INSERT,
                        (SESSION_ID, "T", "active", NOW_ISO, NOW_ISO, 0, 0),
                    )
                    started.set()
                    await asyncio.sleep(3600)

            task = asyncio.create_task(slow_write())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 0
            # lock released after cancellation: new transaction works
            await add_session(db)
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_write_transaction_rolls_back_when_commit_fails(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            # defer_foreign_keys pushes the FK check to COMMIT, so the INSERT
            # succeeds and the IntegrityError surfaces at commit time. This
            # specifically exercises a failure AFTER the transaction body but
            # BEFORE the transaction is finalized, which is the path the
            # write_transaction cleanup must still cover.
            with pytest.raises(sqlite3.IntegrityError):
                async with db.write_transaction() as conn:
                    await conn.execute("PRAGMA defer_foreign_keys=ON")
                    await conn.execute(
                        _RUN_INSERT,
                        (
                            RUN_ID,
                            f"ses_{'9' * 32}",
                            "Created",
                            "in",
                            None,
                            NOW_ISO,
                            None,
                            None,
                            None,
                            "{}",
                        ),
                    )
            # No residue and the connection left the transaction on its own.
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM runs"))["c"] == 0
            assert db._connection.in_transaction is False
            # A subsequent transaction must succeed (no leftover transaction).
            await add_session(db)
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 11. concurrent write_transaction serialization
# --------------------------------------------------------------------------

def test_concurrent_write_transactions_serialize(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            log: list[tuple[str, str]] = []

            async def worker(name: str) -> None:
                async with db.write_transaction() as conn:
                    log.append((name, "start"))
                    await conn.execute(
                        _SESSION_INSERT,
                        (f"ses_{name * 32}", "T", "active", NOW_ISO, NOW_ISO, 0, 0),
                    )
                    await asyncio.sleep(0.02)
                    log.append((name, "end"))

            await asyncio.gather(worker("aa"), worker("bb"))

            # Transaction bodies must not interleave.
            runs = [
                (log[0], log[1]),
                (log[2], log[3]),
            ]
            assert all(start[0] == end[0] for start, end in runs)
            assert len(log) == 4
            row = await db.fetchone("SELECT COUNT(*) AS c FROM sessions")
            assert row["c"] == 2
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 12. schema v1 constraints (behavioral)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["active", "archived"])
def test_sessions_accepts_valid_status(db_path: Path, status: str) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db, status=status)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("status", ["pending", "ACTIVE", "", "deleted", "Archived"])
def test_sessions_rejects_invalid_status(db_path: Path, status: str) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_session(db, status=status)
        finally:
            await db.close()

    _run(scenario())


def test_sessions_rejects_negative_last_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_session(db, last_seq=-1)
        finally:
            await db.close()

    _run(scenario())


def test_sessions_rejects_negative_replay_floor(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_session(db, floor=-1)
        finally:
            await db.close()

    _run(scenario())


def test_sessions_rejects_floor_above_last_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_session(db, last_seq=2, floor=3)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("status", VALID_RUN_STATUSES)
def test_runs_accepts_valid_status(db_path: Path, status: str) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            await add_run(db, status=status)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("status", ["created", "Running", "", "Paused", "Done"])
def test_runs_rejects_invalid_status(db_path: Path, status: str) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                await add_run(db, status=status)
        finally:
            await db.close()

    _run(scenario())


def test_runs_model_snapshot_is_required(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                async with db.write_transaction() as conn:
                    await conn.execute(
                        "INSERT INTO runs(run_id, session_id, status, user_input, "
                        "final_response, created_at, started_at, finished_at, "
                        "failure_json, model_snapshot_json) VALUES "
                        "(?, ?, 'Created', 'in', NULL, ?, NULL, NULL, NULL, NULL)",
                        (RUN_ID, SESSION_ID, NOW_ISO),
                    )
        finally:
            await db.close()

    _run(scenario())


def test_events_rejects_non_positive_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, seq=0)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("retention", ["transient", "Durable", "", "perm"])
def test_events_rejects_invalid_retention(db_path: Path, retention: str) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, retention=retention)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("schema", [0, 2, -1])
def test_events_rejects_wrong_schema_version(db_path: Path, schema: int) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, schema=schema)
        finally:
            await db.close()

    _run(scenario())


def test_events_enforce_unique_session_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            await add_event(db, eid=f"evt_{'1' * 32}", seq=5)
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, eid=f"evt_{'2' * 32}", seq=5)
        finally:
            await db.close()

    _run(scenario())


def test_runtime_state_singleton_id_constrained_to_one(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                async with db.write_transaction() as conn:
                    await conn.execute(
                        "INSERT INTO runtime_state(singleton_id, active_run_id, updated_at) "
                        "VALUES (2, NULL, ?)",
                        (NOW_ISO,),
                    )
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 13. indexes
# --------------------------------------------------------------------------

async def _ai_index_columns(db: RuntimeDatabase, table: str) -> dict[str, list[str]]:
    rows = await db.fetchall(f"PRAGMA index_list({table})")
    result: dict[str, list[str]] = {}
    for row in rows:
        seq = row
        info = await db.fetchall(f"PRAGMA index_info({seq['name']})")
        result[seq["name"]] = [r["name"] for r in info]
    return result


def test_required_indexes_exist(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            runs_idx = await _ai_index_columns(db, "runs")
            assert runs_idx.get("runs_by_session_created") == [
                "session_id",
                "created_at",
                "run_id",
            ]
            events_idx = await _ai_index_columns(db, "events")
            assert "events_for_replay" in events_idx
            assert events_idx["events_for_replay"] == ["session_id", "seq"]
            # UNIQUE(session_id, seq) creates an auto-index on events.
            auto = [
                name
                for name, cols in events_idx.items()
                if cols == ["session_id", "seq"]
            ]
            assert len(auto) >= 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 14. foreign keys and cascades
# --------------------------------------------------------------------------

def test_runs_require_existing_session(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_run(db, sid=f"ses_{'9' * 32}")
        finally:
            await db.close()

    _run(scenario())


def test_events_require_existing_session(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, sid=f"ses_{'9' * 32}")
        finally:
            await db.close()

    _run(scenario())


def test_events_require_existing_run_when_set(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            with pytest.raises(sqlite3.IntegrityError):
                await add_event(db, rid=f"run_{'9' * 32}")
        finally:
            await db.close()

    _run(scenario())


def test_deleting_session_cascades_to_runs_and_events(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            await add_run(db)
            await add_event(db, rid=RUN_ID, seq=1)
            await add_event(db, eid=f"evt_{'3' * 32}", rid=RUN_ID, seq=2)
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM sessions WHERE session_id = ?", (SESSION_ID,))
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM runs"))["c"] == 0
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_deleting_run_cascades_to_events(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            await add_run(db)
            await add_event(db, rid=RUN_ID, seq=1)
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM runs WHERE run_id = ?", (RUN_ID,))
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_deleting_active_run_sets_runtime_state_null(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_session(db)
            await add_run(db)
            async with db.write_transaction() as conn:
                await conn.execute(
                    "UPDATE runtime_state SET active_run_id = ?, updated_at = ? "
                    "WHERE singleton_id = 1",
                    (RUN_ID, NOW_ISO),
                )
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM runs WHERE run_id = ?", (RUN_ID,))
            row = await db.fetchone("SELECT active_run_id FROM runtime_state")
            assert row["active_run_id"] is None
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 15. schema v2 migration checksums (Task 16-B1)
# --------------------------------------------------------------------------

import hashlib  # noqa: E402  (local to the checksum section)


def _expected_checksum(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def _seed_legacy_v1_database(db_path: Path, *, with_session: bool = False) -> None:
    """Build a pre-checksum schema-v1 database the way the old Runtime did.

    The ``schema_migrations`` table has no ``checksum`` column, exactly one v1
    row is applied, and the v1 application tables + runtime_state singleton
    exist. Optionally seeds a session row so migration preservation is tested.
    """
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for statement in migrations_mod.split_sql_statements(migrations_mod.MIGRATION_V1_SQL):
            raw.execute(statement)
        raw.execute(
            "INSERT INTO runtime_state(singleton_id, active_run_id, updated_at) "
            "VALUES (1, NULL, ?)",
            (NOW_ISO,),
        )
        raw.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, NOW_ISO),
        )
        if with_session:
            raw.execute(_SESSION_INSERT, (SESSION_ID, "T", "active", NOW_ISO, NOW_ISO, 0, 0))
        raw.commit()
    finally:
        raw.close()


def test_migration_rows_carry_sha256_checksum_of_script(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            cols = await db.fetchall("PRAGMA table_info(schema_migrations)")
            assert "checksum" in {row["name"] for row in cols}
            rows = await db.fetchall(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            assert [r["version"] for r in rows] == [1, 2, 3, 4]
            expected = {
                v: _expected_checksum(script) for v, script in migrations_mod.MIGRATIONS
            }
            for row in rows:
                assert row["checksum"] == expected[row["version"]]
                assert len(row["checksum"]) == 64
        finally:
            await db.close()

    _run(scenario())


def test_legacy_v1_database_without_checksum_is_upgraded(db_path: Path) -> None:
    _seed_legacy_v1_database(db_path, with_session=True)
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            assert await db.schema_version() == 4
            # checksum column added and v1 backfilled with the known checksum.
            row = await db.fetchone(
                "SELECT checksum FROM schema_migrations WHERE version = 1"
            )
            assert row["checksum"] == _expected_checksum(migrations_mod.MIGRATION_V1_SQL)
            # v2 applied with its own checksum.
            row2 = await db.fetchone(
                "SELECT checksum FROM schema_migrations WHERE version = 2"
            )
            assert row2["checksum"] == _expected_checksum(migrations_mod.MIGRATION_V2_SQL)
            # v1 data preserved; v2/v3/v4 tables now available.
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
            names = await db.table_names()
            assert {
                "workspaces",
                "changesets",
                "approvals",
                "change_receipts",
                "session_workspace_state",
                "artifacts",
            } <= names
        finally:
            await db.close()

    _run(scenario())


def test_legacy_v1_database_round_trips_sessions_runs_events(db_path: Path) -> None:
    _seed_legacy_v1_database(db_path, with_session=True)
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            await add_run(db)
            await add_event(db, rid=RUN_ID, seq=1)
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM runs"))["c"] == 1
            assert (await db.fetchone("SELECT COUNT(*) AS c FROM events"))["c"] == 1
        finally:
            await db.close()

    _run(scenario())


def test_tampered_migration_checksum_fails_closed(db_path: Path) -> None:
    async def setup() -> None:
        db = await RuntimeDatabase.open(db_path)
        await db.close()

    _run(setup())
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("0" * 64,),
        )
        raw.commit()
    finally:
        raw.close()
    with pytest.raises(RuntimeError, match="checksum"):
        _run(RuntimeDatabase.open(db_path))


def test_reopened_database_revalidates_checksums(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        await add_session(db)
        await db.close()
        # A normal reopen must re-read and re-validate every checksum.
        db2 = await RuntimeDatabase.open(db_path)
        try:
            assert await db2.schema_version() == 4
            assert (await db2.fetchone("SELECT COUNT(*) AS c FROM sessions"))["c"] == 1
        finally:
            await db2.close()

    _run(scenario())


def test_failed_v2_migration_leaves_no_partial_tables(db_path: Path) -> None:
    # Seed a v1-only legacy database, then make the v2 script fail partway: it
    # creates one table then errors. The atomic v2 transaction must roll back so
    # no partial v2 schema is visible and the v1 data remains intact.
    _seed_legacy_v1_database(db_path, with_session=True)
    bad_v2 = (
        "CREATE TABLE tmp_v2_partial(x INTEGER);\n"
        "INSERT INTO no_such_table(x) VALUES (1);"
    )
    monkey_v2 = (
        (1, migrations_mod.MIGRATION_V1_SQL),
        (2, bad_v2),
    )
    original = migrations_mod.MIGRATIONS
    migrations_mod.MIGRATIONS = monkey_v2  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.Error):
            _run(RuntimeDatabase.open(db_path))
    finally:
        migrations_mod.MIGRATIONS = original  # type: ignore[assignment]

    raw = sqlite3.connect(db_path)
    try:
        tables = {
            r[0]
            for r in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        versions = [
            r[0] for r in raw.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        sessions = raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        raw.close()
    assert "tmp_v2_partial" not in tables
    assert "changesets" not in tables
    assert versions == [1]  # v2 not recorded
    assert sessions == 1  # v1 data intact


# --------------------------------------------------------------------------
# 16. schema v3 active-workspace structure (Task 16-B2b)
# --------------------------------------------------------------------------


async def _add_raw_workspace(
    db: RuntimeDatabase,
    *,
    workspace_id: str,
    session_id: str,
    run_id: str,
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "INSERT INTO workspaces(workspace_id, session_id, instance_id, "
            "scene_epoch, revision, created_by_run, updated_at, digest, "
            "payload_json, schema_version) VALUES (?, ?, 'inst', 1, ?, ?, ?, ?, '{}', 1)",
            (workspace_id, session_id, "a" * 64, run_id, NOW_ISO, "b" * 64),
        )


def test_schema_v3_composite_active_workspace_fk_is_session_scoped(
    db_path: Path,
) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            other_session = f"ses_{'1' * 32}"
            other_run = f"run_{'1' * 32}"
            workspace = f"ws_{'2' * 32}"
            await add_session(db)
            await add_run(db)
            await add_session(db, other_session)
            await add_run(db, other_run, other_session)
            await _add_raw_workspace(
                db,
                workspace_id=workspace,
                session_id=SESSION_ID,
                run_id=RUN_ID,
            )
            with pytest.raises(sqlite3.IntegrityError):
                async with db.write_transaction() as conn:
                    await conn.execute(
                        "INSERT INTO session_workspace_state(session_id, "
                        "active_workspace_id, state_revision, updated_at) "
                        "VALUES (?, ?, 1, ?)",
                        (other_session, workspace, NOW_ISO),
                    )
        finally:
            await db.close()

    _run(scenario())


def test_schema_v3_preserves_accepted_v1_v2_script_checksums() -> None:
    assert migrations_mod.migration_checksum(migrations_mod.MIGRATION_V1_SQL) == (
        "bcbabc5b6dd01a18e34e2c42ca329c46c018e596b96a9131707725c78ea2206f"
    )
    assert migrations_mod.migration_checksum(migrations_mod.MIGRATION_V2_SQL) == (
        "12e07ebd08d74b3a7f90f92853585b75d4ad3df562ec2ae1d4c8f69e87723e77"
    )


def test_existing_schema_v2_database_upgrades_to_v3_once(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
        )
        for version, script in migrations_mod.MIGRATIONS[:2]:
            for statement in migrations_mod.split_sql_statements(script):
                raw.execute(statement)
            if version == 1:
                raw.execute(
                    "INSERT INTO runtime_state(singleton_id, active_run_id, updated_at) "
                    "VALUES (1, NULL, ?)",
                    (NOW_ISO,),
                )
            raw.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (?, ?, ?)",
                (version, NOW_ISO, migrations_mod.migration_checksum(script)),
            )
        raw.execute(
            _SESSION_INSERT,
            (SESSION_ID, "preserved", "active", NOW_ISO, NOW_ISO, 0, 0),
        )
        raw.commit()
    finally:
        raw.close()

    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            assert await db.schema_version() == 4
            assert (
                await db.fetchone(
                    "SELECT title FROM sessions WHERE session_id = ?", (SESSION_ID,)
                )
            )["title"] == "preserved"
            rows = await db.fetchall(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            assert [row["version"] for row in rows] == [1, 2, 3, 4]
            assert (
                await db.fetchone(
                    "SELECT COUNT(*) AS c FROM session_workspace_state"
                )
            )["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_schema_v3_state_cascades_with_session(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            workspace = f"ws_{'2' * 32}"
            await add_session(db)
            await add_run(db)
            await _add_raw_workspace(
                db,
                workspace_id=workspace,
                session_id=SESSION_ID,
                run_id=RUN_ID,
            )
            async with db.write_transaction() as conn:
                await conn.execute(
                    "INSERT INTO session_workspace_state(session_id, "
                    "active_workspace_id, state_revision, updated_at) "
                    "VALUES (?, ?, 1, ?)",
                    (SESSION_ID, workspace, NOW_ISO),
                )
                await conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (SESSION_ID,)
                )
            assert (
                await db.fetchone(
                    "SELECT COUNT(*) AS c FROM session_workspace_state"
                )
            )["c"] == 0
        finally:
            await db.close()

    _run(scenario())


def test_failed_v3_migration_rolls_back_index_and_table(db_path: Path) -> None:
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
        )
        for version, script in migrations_mod.MIGRATIONS[:2]:
            for statement in migrations_mod.split_sql_statements(script):
                raw.execute(statement)
            if version == 1:
                raw.execute(
                    "INSERT INTO runtime_state(singleton_id, active_run_id, updated_at) "
                    "VALUES (1, NULL, ?)",
                    (NOW_ISO,),
                )
            raw.execute(
                "INSERT INTO schema_migrations(version, applied_at, checksum) "
                "VALUES (?, ?, ?)",
                (version, NOW_ISO, migrations_mod.migration_checksum(script)),
            )
        raw.commit()
    finally:
        raw.close()

    bad_v3 = (
        "CREATE UNIQUE INDEX workspaces_identity_by_session "
        "ON workspaces(workspace_id, session_id);\n"
        "CREATE TABLE tmp_v3_partial(x INTEGER);\n"
        "INSERT INTO no_such_table(x) VALUES (1);"
    )
    original = migrations_mod.MIGRATIONS
    migrations_mod.MIGRATIONS = (*original[:2], (3, bad_v3))  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.Error):
            _run(RuntimeDatabase.open(db_path))
    finally:
        migrations_mod.MIGRATIONS = original  # type: ignore[assignment]

    raw = sqlite3.connect(db_path)
    try:
        names = {
            row[0]
            for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
            ).fetchall()
        }
        versions = [
            row[0]
            for row in raw.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
    finally:
        raw.close()
    assert "workspaces_identity_by_session" not in names
    assert "tmp_v3_partial" not in names
    assert versions == [1, 2]
