from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from eee_agent.core import AgentException, ErrorCategory
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import SessionRecord, SessionStatus
from eee_agent.runtime.sessions import SessionRepository

RUN_ID = f"run_{'0' * 32}"
EVENT_ID = f"evt_{'0' * 32}"
MISSING_ID = f"ses_{'a' * 32}"
NOW_ISO = "2026-07-14T02:30:00+00:00"

_RUN_INSERT = (
    "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
    "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
    "VALUES (?, ?, 'Created', 'in', NULL, ?, NULL, NULL, NULL, '{}')"
)
_EVENT_INSERT = (
    "INSERT INTO events(event_id, session_id, run_id, seq, event_type, timestamp, "
    "payload_json, retention_class, schema_version) "
    "VALUES (?, ?, ?, 1, 'run.test', ?, '{}', 'durable', 1)"
)


def _run(coro):
    return asyncio.run(coro)


async def _open(db_path: Path) -> tuple[RuntimeDatabase, SessionRepository]:
    db = await RuntimeDatabase.open(db_path)
    return db, SessionRepository(db)


async def _insert_run(db: RuntimeDatabase, run_id: str, session_id: str) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(_RUN_INSERT, (run_id, session_id, NOW_ISO))


async def _insert_event(
    db: RuntimeDatabase, event_id: str, session_id: str, run_id: str
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(_EVENT_INSERT, (event_id, session_id, run_id, NOW_ISO))


async def _set_active_run(db: RuntimeDatabase, run_id: str) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "UPDATE runtime_state SET active_run_id = ?, updated_at = ? "
            "WHERE singleton_id = 1",
            (run_id, NOW_ISO),
        )


async def _count(
    db: RuntimeDatabase, table: str, where: str = "", params: tuple = ()
) -> int:
    sql = f"SELECT COUNT(*) AS c FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return (await db.fetchone(sql, params))["c"]


# --------------------------------------------------------------------------
# 1. create
# --------------------------------------------------------------------------

def test_create_returns_normalized_active_session(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("  Table  ")
            assert type(session) is SessionRecord
            assert session.title == "Table"
            assert session.session_id.startswith("ses_")
            assert session.status is SessionStatus.ACTIVE
            assert session.created_at.tzinfo is not None
            assert session.updated_at.tzinfo is not None
            assert session.created_at == session.updated_at
            assert session.last_seq == 0
            assert session.replay_floor_seq == 0
            assert (await repo.get(session.session_id)).title == "Table"
            assert await _count(db, "events", "session_id = ?", (session.session_id,)) == 0
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 2. create survives reopen
# --------------------------------------------------------------------------

def test_create_survives_reopen(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        created = await repo.create("Chair")
        await db.close()
        db2, repo2 = await _open(db_path)
        try:
            loaded = await repo2.get(created.session_id)
            assert loaded.session_id == created.session_id
            assert loaded.title == "Chair"
            assert loaded.status is SessionStatus.ACTIVE
            assert loaded.created_at == created.created_at
            assert loaded.updated_at == created.updated_at
            assert loaded.last_seq == 0
            assert loaded.replay_floor_seq == 0
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 3/4. list
# --------------------------------------------------------------------------

def test_list_defaults_to_active(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            a = await repo.create("A")
            b = await repo.create("B")
            await repo.archive(a.session_id)
            result = await repo.list()
            assert all(type(s) is SessionRecord for s in result)
            ids = {s.session_id for s in result}
            assert b.session_id in ids
            assert a.session_id not in ids
        finally:
            await db.close()

    _run(scenario())


def test_list_includes_archived_when_requested(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            a = await repo.create("A")
            b = await repo.create("B")
            await repo.archive(a.session_id)
            result = await repo.list(include_archived=True)
            ids = {s.session_id for s in result}
            assert ids == {a.session_id, b.session_id}
            assert len(result) == len(ids)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_list_rejects_non_bool_include_archived(db_path: Path, value: object) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(TypeError):
                await repo.list(include_archived=value)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 5. rename
# --------------------------------------------------------------------------

def test_rename_updates_title_and_timestamp(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("Old")
            renamed = await repo.rename(created.session_id, "  New Title  ")
            assert renamed.title == "New Title"
            assert renamed.created_at == created.created_at
            assert renamed.updated_at >= created.updated_at
            assert renamed.status is SessionStatus.ACTIVE
            assert renamed.last_seq == 0
            assert renamed.replay_floor_seq == 0
            assert (await repo.get(created.session_id)).title == "New Title"
            assert await _count(db, "events", "session_id = ?", (created.session_id,)) == 0
        finally:
            await db.close()

    _run(scenario())


def test_rename_archived_session_stays_archived(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("Old")
            await repo.archive(created.session_id)
            renamed = await repo.rename(created.session_id, "New")
            assert renamed.title == "New"
            assert renamed.status is SessionStatus.ARCHIVED
        finally:
            await db.close()

    _run(scenario())


def test_rename_persists_after_reopen(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        created = await repo.create("Old")
        await repo.rename(created.session_id, "Persisted")
        await db.close()
        db2, repo2 = await _open(db_path)
        try:
            assert (await repo2.get(created.session_id)).title == "Persisted"
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 6. archive
# --------------------------------------------------------------------------

def test_archive_transitions_to_archived(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("A")
            archived = await repo.archive(created.session_id)
            assert archived.status is SessionStatus.ARCHIVED
            assert archived.title == "A"
            assert archived.created_at == created.created_at
            assert archived.last_seq == 0
            assert archived.replay_floor_seq == 0
            assert archived.updated_at >= created.updated_at
            assert created.session_id not in {s.session_id for s in await repo.list()}
            assert created.session_id in {
                s.session_id for s in await repo.list(include_archived=True)
            }
            assert await _count(db, "events", "session_id = ?", (created.session_id,)) == 0
        finally:
            await db.close()

    _run(scenario())


def test_archive_is_idempotent(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("A")
            first = await repo.archive(created.session_id)
            second = await repo.archive(created.session_id)
            assert second.status is SessionStatus.ARCHIVED
            assert second.updated_at == first.updated_at
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 7. delete_application_records
# --------------------------------------------------------------------------

def test_delete_application_records_cascades(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("A")
            await _insert_run(db, RUN_ID, session.session_id)
            await _insert_event(db, EVENT_ID, session.session_id, RUN_ID)
            result = await repo.delete_application_records(session.session_id)
            assert result is None
            assert await _count(db, "sessions", "session_id = ?", (session.session_id,)) == 0
            assert await _count(db, "runs") == 0
            assert await _count(db, "events") == 0
            rs = await db.fetchone(
                "SELECT singleton_id, active_run_id FROM runtime_state"
            )
            assert rs["singleton_id"] == 1
            assert rs["active_run_id"] is None
        finally:
            await db.close()

    _run(scenario())


def test_delete_application_records_persists_after_reopen(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        session = await repo.create("A")
        await repo.delete_application_records(session.session_id)
        await db.close()
        db2, repo2 = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await repo2.get(session.session_id)
            assert exc.value.error.code == "runtime.session_not_found"
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 8. missing session errors
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method", ["get", "rename", "archive", "delete_application_records"]
)
def test_missing_session_raises_session_not_found(db_path: Path, method: str) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                if method == "get":
                    await repo.get(MISSING_ID)
                elif method == "rename":
                    await repo.rename(MISSING_ID, "X")
                elif method == "archive":
                    await repo.archive(MISSING_ID)
                else:
                    await repo.delete_application_records(MISSING_ID)
            error = exc.value.error
            assert error.code == "runtime.session_not_found"
            assert error.category is ErrorCategory.VALIDATION
            assert error.message_for_user == "The Runtime session does not exist."
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 9. ID kind / format / type validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_id",
    [f"run_{'0' * 32}", f"evt_{'0' * 32}", "ses_short", "not-an-id"],
)
def test_wrong_kind_or_malformed_id_rejected(db_path: Path, bad_id: str) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await repo.get(bad_id)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_id", [123, None, object()])
def test_non_string_id_rejected(db_path: Path, bad_id: object) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(TypeError):
                await repo.get(bad_id)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_methods_validate_id_before_database_write(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("Real")
            bad = f"run_{'0' * 32}"
            with pytest.raises(ValueError):
                await repo.rename(bad, "X")
            with pytest.raises(ValueError):
                await repo.archive(bad)
            with pytest.raises(ValueError):
                await repo.delete_application_records(bad)
            loaded = await repo.get(created.session_id)
            assert loaded.title == "Real"
            assert loaded.status is SessionStatus.ACTIVE
            assert await _count(db, "sessions") == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 10. title normalization and boundaries
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title", ["", "   ", "\t\n "])
def test_create_rejects_blank_title(db_path: Path, title: str) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await repo.create(title)
            assert await _count(db, "sessions") == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("title", [123, None, ["list"]])
def test_create_rejects_non_string_title(db_path: Path, title: object) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(TypeError):
                await repo.create(title)  # type: ignore[arg-type]
            assert await _count(db, "sessions") == 0
        finally:
            await db.close()

    _run(scenario())


def test_create_accepts_200_codepoint_title(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("a" * 200)
            assert session.title == "a" * 200
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("title", ["a" * 201, "😀" * 201])
def test_create_rejects_oversized_title(db_path: Path, title: str) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await repo.create(title)
            assert exc.value.error.code == "validation.payload_too_large"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _count(db, "sessions") == 0
        finally:
            await db.close()

    _run(scenario())


def test_create_counts_multibyte_by_code_points(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("😀" * 200)
            assert session.title == "😀" * 200
            assert len(session.title) == 200
        finally:
            await db.close()

    _run(scenario())


def test_rename_uses_same_title_validation(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            created = await repo.create("Original")
            with pytest.raises(AgentException):
                await repo.rename(created.session_id, "a" * 201)
            with pytest.raises(ValueError):
                await repo.rename(created.session_id, "   ")
            loaded = await repo.get(created.session_id)
            assert loaded.title == "Original"
            assert loaded.updated_at == created.updated_at
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 11/12. active run blocks archive / delete
# --------------------------------------------------------------------------

def test_active_run_blocks_archive(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("A")
            await _insert_run(db, RUN_ID, session.session_id)
            await _set_active_run(db, RUN_ID)
            with pytest.raises(AgentException) as exc:
                await repo.archive(session.session_id)
            assert exc.value.error.code == "runtime.run_already_active"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            loaded = await repo.get(session.session_id)
            assert loaded.status is SessionStatus.ACTIVE
            assert loaded.updated_at == session.updated_at
            assert await _count(db, "runs") == 1
            assert (await db.fetchone("SELECT active_run_id FROM runtime_state"))[
                "active_run_id"
            ] == RUN_ID
            assert await _count(db, "events") == 0
        finally:
            await db.close()

    _run(scenario())


def test_active_run_blocks_delete(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("A")
            await _insert_run(db, RUN_ID, session.session_id)
            await _insert_event(db, EVENT_ID, session.session_id, RUN_ID)
            await _set_active_run(db, RUN_ID)
            with pytest.raises(AgentException) as exc:
                await repo.delete_application_records(session.session_id)
            assert exc.value.error.code == "runtime.run_already_active"
            assert await _count(db, "sessions") == 1
            assert await _count(db, "runs") == 1
            assert await _count(db, "events") == 1
            assert (await db.fetchone("SELECT active_run_id FROM runtime_state"))[
                "active_run_id"
            ] == RUN_ID
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 13. another session's active run does not block the current session
# --------------------------------------------------------------------------

def test_other_session_active_run_does_not_block_current(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            a = await repo.create("A")
            b = await repo.create("B")
            await _insert_run(db, RUN_ID, a.session_id)
            await _set_active_run(db, RUN_ID)
            archived_b = await repo.archive(b.session_id)
            assert archived_b.status is SessionStatus.ARCHIVED
            await repo.delete_application_records(b.session_id)
            assert (await repo.get(a.session_id)).status is SessionStatus.ACTIVE
            assert await _count(db, "runs") == 1
            assert (await db.fetchone("SELECT active_run_id FROM runtime_state"))[
                "active_run_id"
            ] == RUN_ID
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 14. strict frozen SessionRecord
# --------------------------------------------------------------------------

def test_returned_records_are_frozen_session_records(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("A")
            assert type(session) is SessionRecord
            with pytest.raises(AttributeError):
                session.title = "mutate"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 15. repository never writes events or advances sequence
# --------------------------------------------------------------------------

def test_repository_never_writes_events_or_advances_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            session = await repo.create("A")
            await repo.rename(session.session_id, "B")
            await repo.archive(session.session_id)
            assert await _count(db, "events") == 0
            row = await db.fetchone(
                "SELECT last_seq, replay_floor_seq FROM sessions WHERE session_id = ?",
                (session.session_id,),
            )
            assert row["last_seq"] == 0
            assert row["replay_floor_seq"] == 0
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 16. concurrent TOCTOU: return a transaction-local snapshot, not a post-commit re-read
# --------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["create", "rename", "archive"])
def test_mutation_returns_committed_snapshot_under_concurrent_delete(
    db_path: Path, op: str
) -> None:
    async def scenario() -> None:
        db, repo = await _open(db_path)
        try:
            seed: SessionRecord | None = None
            if op in ("rename", "archive"):
                seed = await repo.create("seed")

            original_write_transaction = db.write_transaction
            mutation_committed = asyncio.Event()
            allow_return = asyncio.Event()
            paused = False

            @asynccontextmanager
            async def pause_after_commit():
                nonlocal paused
                # The real transaction runs unchanged; only AFTER it has exited
                # (COMMIT done, write lock released) do we pause, which is the
                # exact window between commit and the repository's return.
                async with original_write_transaction() as conn:
                    yield conn
                if not paused:
                    paused = True
                    mutation_committed.set()
                    await allow_return.wait()

            db.write_transaction = pause_after_commit

            if op == "create":
                task = asyncio.create_task(repo.create("race"))
            elif op == "rename":
                assert seed is not None
                task = asyncio.create_task(repo.rename(seed.session_id, "renamed"))
            else:
                assert seed is not None
                task = asyncio.create_task(repo.archive(seed.session_id))

            await mutation_committed.wait()

            if op == "create":
                committed_row = await db.fetchone(
                    "SELECT session_id FROM sessions WHERE title = 'race'"
                )
                assert committed_row is not None
                session_id = committed_row["session_id"]
            else:
                assert seed is not None
                session_id = seed.session_id

            # Prove the mutation itself committed before the pause.
            state = await db.fetchone(
                "SELECT title, status FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            assert state is not None
            if op == "rename":
                assert state["title"] == "renamed"
            elif op == "archive":
                assert state["status"] == "archived"

            # Concurrently delete the just-committed session.
            await SessionRepository(db).delete_application_records(session_id)
            assert await _count(db, "sessions", "session_id = ?", (session_id,)) == 0

            allow_return.set()
            result = await task

            # The mutator must return its own committed snapshot, not re-read a
            # now-deleted row (which would surface runtime.session_not_found).
            assert result.session_id == session_id
            assert result.last_seq == 0
            assert result.replay_floor_seq == 0
            if op == "create":
                assert result.title == "race"
                assert result.status is SessionStatus.ACTIVE
            elif op == "rename":
                assert result.title == "renamed"
                assert result.status is SessionStatus.ACTIVE
            else:
                assert result.status is SessionStatus.ARCHIVED
        finally:
            await db.close()

    _run(scenario())
