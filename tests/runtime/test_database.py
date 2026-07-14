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
# 1. empty database -> schema v1
# --------------------------------------------------------------------------

def test_empty_database_reaches_schema_v1(db_path: Path) -> None:
    async def scenario() -> None:
        db = await RuntimeDatabase.open(db_path)
        try:
            assert await db.schema_version() == 1
            names = await db.table_names()
            assert {
                "schema_migrations",
                "sessions",
                "runs",
                "events",
                "runtime_state",
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
            assert await db2.schema_version() == 1
            rows = await db2.fetchall(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
            assert [r["version"] for r in rows] == [1]
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
        [2],
    )
    with pytest.raises(RuntimeError, match="newer than this Runtime"):
        _run(RuntimeDatabase.open(db_path))

    # The failed open() must have released the file handle (Windows-safe).
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("SELECT version FROM schema_migrations").fetchone()[0] == 2
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
        [4],
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
