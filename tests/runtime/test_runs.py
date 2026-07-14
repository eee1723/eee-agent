from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import (
    RunRecord,
    RunStatus,
    SessionStatus,
    canonical_json_dumps,
    canonical_json_loads,
)
from eee_agent.runtime.runs import RunRepository
from eee_agent.runtime.sessions import SessionRepository

RUN_ID = f"run_{'0' * 32}"
EVENT_ID = f"evt_{'0' * 32}"
MISSING_SESSION = f"ses_{'a' * 32}"
MISSING_RUN = f"run_{'b' * 32}"
NOW_ISO = "2026-07-14T02:30:00+00:00"
STARTED_ISO = "2026-07-14T03:00:00+00:00"

MAX_USER_INPUT_BYTES = 262_144
MAX_FINAL_RESPONSE_BYTES = 1_048_576

_RUN_INSERT = (
    "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
    "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

LEGAL_EDGES = [
    (RunStatus.CREATED, RunStatus.PREPARING_CONTEXT),
    (RunStatus.CREATED, RunStatus.STOP_REQUESTED),
    (RunStatus.CREATED, RunStatus.FAILED),
    (RunStatus.PREPARING_CONTEXT, RunStatus.PLANNING),
    (RunStatus.PREPARING_CONTEXT, RunStatus.STOP_REQUESTED),
    (RunStatus.PREPARING_CONTEXT, RunStatus.RETRYING),
    (RunStatus.PREPARING_CONTEXT, RunStatus.FAILED),
    (RunStatus.PLANNING, RunStatus.FINALIZING),
    (RunStatus.PLANNING, RunStatus.STOP_REQUESTED),
    (RunStatus.PLANNING, RunStatus.RETRYING),
    (RunStatus.PLANNING, RunStatus.FAILED),
    (RunStatus.RETRYING, RunStatus.PREPARING_CONTEXT),
    (RunStatus.RETRYING, RunStatus.PLANNING),
    (RunStatus.RETRYING, RunStatus.STOP_REQUESTED),
    (RunStatus.RETRYING, RunStatus.FAILED),
    (RunStatus.FINALIZING, RunStatus.COMPLETED),
    (RunStatus.FINALIZING, RunStatus.STOP_REQUESTED),
    (RunStatus.FINALIZING, RunStatus.FAILED),
    (RunStatus.STOP_REQUESTED, RunStatus.STOPPING),
    (RunStatus.STOPPING, RunStatus.CANCELLED),
    (RunStatus.STOPPING, RunStatus.FAILED),
]


def _run(coro):
    return asyncio.run(coro)


async def _open(db_path: Path):
    db = await RuntimeDatabase.open(db_path)
    return db, SessionRepository(db), RunRepository(db)


async def _seed_run(
    db: RuntimeDatabase,
    session_id: str,
    status: str,
    run_id: str = RUN_ID,
    started: str | None = None,
    finished: str | None = None,
    failure: str | None = None,
    model: str = '{"model":"fake"}',
    user_input: str = "in",
    final: str | None = None,
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _RUN_INSERT,
            (run_id, session_id, status, user_input, final, NOW_ISO, started, finished, failure, model),
        )


async def _set_active_run(db: RuntimeDatabase, run_id: str) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "UPDATE runtime_state SET active_run_id = ?, updated_at = ? WHERE singleton_id = 1",
            (run_id, NOW_ISO),
        )


async def _count(db: RuntimeDatabase, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT COUNT(*) AS c FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return (await db.fetchone(sql, params))["c"]


# --------------------------------------------------------------------------
# canonical JSON helpers (Task 5/6 shared)
# --------------------------------------------------------------------------

def test_canonical_json_dumps_sorted_compact_unicode() -> None:
    text = canonical_json_dumps({"b": 1, "a": "中文", "c": True, "d": None, "e": [3, 2, 1]})
    assert text == '{"a":"中文","b":1,"c":true,"d":null,"e":[3,2,1]}'


def test_canonical_json_dumps_key_order_independent() -> None:
    assert canonical_json_dumps({"x": 1, "y": 2}) == canonical_json_dumps({"y": 2, "x": 1})


def test_canonical_json_dumps_bool_stays_bool() -> None:
    assert canonical_json_dumps({"k": True, "n": 1}) == '{"k":true,"n":1}'


def test_canonical_json_dumps_rejects_non_finite_float() -> None:
    with pytest.raises(ValueError):
        canonical_json_dumps({"x": float("nan")})


def test_canonical_json_dumps_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError):
        canonical_json_dumps({1: "v"})


def test_canonical_json_dumps_rejects_unsupported_values() -> None:
    with pytest.raises(TypeError):
        canonical_json_dumps({"x": (1, 2)})


def test_canonical_json_dumps_rejects_cycles() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        canonical_json_dumps(cyclic)


def test_canonical_json_loads_round_trips() -> None:
    assert canonical_json_loads('{"a":1,"b":[true,"x"]}') == {"a": 1, "b": [True, "x"]}


def test_canonical_json_loads_rejects_malformed() -> None:
    with pytest.raises(ValueError):
        canonical_json_loads("{not json")


def test_canonical_json_loads_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        canonical_json_loads(123)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 1. create_and_acquire basic flow
# --------------------------------------------------------------------------

def test_create_and_acquire_returns_created_run(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(
                session.session_id, "inspect", {"model": "fake", "n": [1, 2]}
            )
            assert type(run) is RunRecord
            assert run.run_id.startswith("run_")
            assert run.session_id == session.session_id
            assert run.status is RunStatus.CREATED
            assert run.user_input == "inspect"
            assert run.final_response is None
            assert run.created_at.tzinfo is not None
            assert run.started_at is None
            assert run.finished_at is None
            assert run.failure_json is None
            assert run.to_dict()["model_snapshot_json"] == {"model": "fake", "n": [1, 2]}
            assert await runs.active_run_id() == run.run_id
            assert await _count(db, "runs") == 1
        finally:
            await db.close()

    _run(scenario())


def test_create_and_acquire_survives_reopen(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        session = await sessions.create("A")
        run = await runs.create_and_acquire(session.session_id, "inspect", {"model": "fake"})
        await db.close()
        db2, _sessions, runs2 = await _open(db_path)
        try:
            loaded = await runs2.get(run.run_id)
            assert loaded.run_id == run.run_id
            assert loaded.status is RunStatus.CREATED
            assert loaded.model_snapshot_json == {"model": "fake"}
            assert await runs2.active_run_id() == run.run_id
        finally:
            await db2.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 2. session validation
# --------------------------------------------------------------------------

def test_create_and_acquire_missing_session(db_path: Path) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await runs.create_and_acquire(MISSING_SESSION, "in", {"m": 1})
            assert exc.value.error.code == "runtime.session_not_found"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _count(db, "runs") == 0
            assert (await db.fetchone("SELECT active_run_id FROM runtime_state"))[
                "active_run_id"
            ] is None
        finally:
            await db.close()

    _run(scenario())


def test_create_and_acquire_archived_session(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await sessions.archive(session.session_id)
            with pytest.raises(AgentException) as exc:
                await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            assert exc.value.error.code == "runtime.session_archived"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    "bad_id", [RUN_ID, EVENT_ID, "ses_short", "not-an-id"]
)
def test_create_and_acquire_rejects_bad_session_id(db_path: Path, bad_id: str) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await runs.create_and_acquire(bad_id, "in", {"m": 1})
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_id", [123, None, object()])
def test_create_and_acquire_rejects_non_string_session_id(
    db_path: Path, bad_id: object
) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(TypeError):
                await runs.create_and_acquire(bad_id, "in", {"m": 1})  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 3. user_input boundaries (UTF-8 bytes)
# --------------------------------------------------------------------------

def test_create_and_acquire_rejects_empty_user_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(ValueError):
                await runs.create_and_acquire(session.session_id, "", {"m": 1})
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


def test_create_and_acquire_rejects_non_string_user_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(TypeError):
                await runs.create_and_acquire(session.session_id, 123, {"m": 1})  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_create_and_acquire_accepts_max_ascii_user_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(
                session.session_id, "a" * MAX_USER_INPUT_BYTES, {"m": 1}
            )
            assert len(run.user_input) == MAX_USER_INPUT_BYTES
        finally:
            await db.close()

    _run(scenario())


def test_create_and_acquire_rejects_oversized_ascii_user_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(AgentException) as exc:
                await runs.create_and_acquire(
                    session.session_id, "a" * (MAX_USER_INPUT_BYTES + 1), {"m": 1}
                )
            assert exc.value.error.code == "validation.payload_too_large"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


def test_user_input_counted_by_utf8_bytes_accept_boundary(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            # "é" is 1 code point but 2 UTF-8 bytes: 131072 * 2 = 262144 bytes.
            run = await runs.create_and_acquire(session.session_id, "é" * 131072, {"m": 1})
            assert run.user_input == "é" * 131072
        finally:
            await db.close()

    _run(scenario())


def test_user_input_counted_by_utf8_bytes_reject_over(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(AgentException) as exc:
                # 131073 * 2 = 262146 > 262144 bytes.
                await runs.create_and_acquire(session.session_id, "é" * 131073, {"m": 1})
            assert exc.value.error.code == "validation.payload_too_large"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 4. model_snapshot JSON
# --------------------------------------------------------------------------

def test_create_and_acquire_snapshots_model_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            source = {"model": "fake", "nested": {"x": [1, 2]}}
            run = await runs.create_and_acquire(session.session_id, "in", source)
            source["model"] = "mutated"
            source["nested"]["x"].append(3)
            assert run.to_dict()["model_snapshot_json"] == {
                "model": "fake",
                "nested": {"x": [1, 2]},
            }
            assert (await runs.get(run.run_id)).to_dict()["model_snapshot_json"] == {
                "model": "fake",
                "nested": {"x": [1, 2]},
            }
            with pytest.raises(TypeError):
                run.model_snapshot_json["model"] = "x"
        finally:
            await db.close()

    _run(scenario())


def test_model_snapshot_persisted_as_canonical_json(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await runs.create_and_acquire(session.session_id, "in", {"b": 1, "a": 2})
            row = await db.fetchone("SELECT model_snapshot_json FROM runs")
            assert row["model_snapshot_json"] == '{"a":2,"b":1}'
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("snapshot", [[], {1: "v"}], ids=["list", "int-key"])
def test_create_rejects_non_object_model_snapshot(db_path: Path, snapshot: object) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises((TypeError, ValueError)):
                await runs.create_and_acquire(session.session_id, "in", snapshot)  # type: ignore[arg-type]
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_create_rejects_non_finite_in_snapshot(db_path: Path, value: float) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(ValueError):
                await runs.create_and_acquire(session.session_id, "in", {"x": value})
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    "value",
    [{1, 2}, (1, 2), datetime(2026, 7, 14, tzinfo=timezone.utc)],
    ids=["set", "tuple", "datetime"],
)
def test_create_rejects_unsupported_in_snapshot(db_path: Path, value: object) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(TypeError):
                await runs.create_and_acquire(session.session_id, "in", {"x": value})
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


def test_create_rejects_cycle_in_snapshot(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            cyclic: dict = {}
            cyclic["self"] = cyclic
            with pytest.raises(ValueError):
                await runs.create_and_acquire(session.session_id, "in", cyclic)
            assert await _count(db, "runs") == 0
        finally:
            await db.close()

    _run(scenario())


def test_model_snapshot_preserves_bool_and_unicode(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(
                session.session_id, "in", {"flag": True, "n": 1, "text": "中文"}
            )
            assert run.model_snapshot_json == {"flag": True, "n": 1, "text": "中文"}
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 5. global active-run atomic ownership
# --------------------------------------------------------------------------

def test_concurrent_acquire_only_one_wins(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            a = await sessions.create("A")
            b = await sessions.create("B")
            results = await asyncio.gather(
                runs.create_and_acquire(a.session_id, "in", {"m": 1}),
                runs.create_and_acquire(b.session_id, "in", {"m": 2}),
                return_exceptions=True,
            )
            successes = [r for r in results if type(r) is RunRecord]
            failures = [r for r in results if isinstance(r, AgentException)]
            assert len(successes) == 1
            assert len(failures) == 1
            assert failures[0].error.code == "runtime.run_already_active"
            assert failures[0].error.category is ErrorCategory.VALIDATION
            assert await _count(db, "runs") == 1
            assert await runs.active_run_id() == successes[0].run_id
        finally:
            await db.close()

    _run(scenario())


def test_second_acquire_for_same_session_blocked(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            with pytest.raises(AgentException) as exc:
                await runs.create_and_acquire(session.session_id, "in2", {"m": 2})
            assert exc.value.error.code == "runtime.run_already_active"
            assert await _count(db, "runs") == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 6. get
# --------------------------------------------------------------------------

def test_get_returns_strict_run_record(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            loaded = await runs.get(run.run_id)
            assert type(loaded) is RunRecord
            assert loaded == run
            with pytest.raises(AttributeError):
                loaded.status = RunStatus.PLANNING
        finally:
            await db.close()

    _run(scenario())


def test_get_missing_run_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await runs.get(MISSING_RUN)
            assert exc.value.error.code == "runtime.run_not_found"
            assert exc.value.error.category is ErrorCategory.VALIDATION
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_id", [f"ses_{'0' * 32}", EVENT_ID, "run_short", "x"])
def test_get_rejects_bad_run_id(db_path: Path, bad_id: str) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await runs.get(bad_id)
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 7. list_for_session
# --------------------------------------------------------------------------

def test_list_for_session_orders_and_returns_tuple(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Created", run_id=f"run_{'1' * 32}", model='{"i":2}')
            await _seed_run(db, session.session_id, "Completed", run_id=f"run_{'2' * 32}", model='{"i":1}')
            result = await runs.list_for_session(session.session_id)
            assert type(result) is tuple
            assert all(type(r) is RunRecord for r in result)
            assert len(result) == 2
            assert [r.run_id for r in result] == [f"run_{'1' * 32}", f"run_{'2' * 32}"]
        finally:
            await db.close()

    _run(scenario())


def test_list_for_session_missing_session_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await runs.list_for_session(MISSING_SESSION)
            assert exc.value.error.code == "runtime.session_not_found"
        finally:
            await db.close()

    _run(scenario())


def test_list_for_session_archived_history(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Completed", run_id=f"run_{'9' * 32}")
            await sessions.archive(session.session_id)
            result = await runs.list_for_session(session.session_id)
            assert len(result) == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 8. active_run_id lifecycle
# --------------------------------------------------------------------------

def test_active_run_id_lifecycle(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            assert await runs.active_run_id() is None
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            assert await runs.active_run_id() == run.run_id
            # nonterminal transition keeps the active slot
            await runs.transition(run.run_id, RunStatus.PREPARING_CONTEXT)
            assert await runs.active_run_id() == run.run_id
            # terminal transition releases the active slot
            await runs.fail(
                run.run_id,
                AgentError("runtime.x", ErrorCategory.INTERNAL_INVARIANT, "x"),
            )
            assert await runs.active_run_id() is None
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 9/10/11/12/13/14. transition
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("current", "target"), LEGAL_EDGES)
def test_transition_legal_edge(
    db_path: Path, current: RunStatus, target: RunStatus
) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, current.value)
            result = await runs.transition(RUN_ID, target)
            assert result.status is target
            assert (await runs.get(RUN_ID)).status is target
        finally:
            await db.close()

    _run(scenario())


def test_transition_sets_started_at_on_first_active(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Created")
            result = await runs.transition(RUN_ID, RunStatus.PREPARING_CONTEXT)
            assert result.started_at is not None
            assert result.started_at.tzinfo is not None
            assert result.started_at >= result.created_at
        finally:
            await db.close()

    _run(scenario())


def test_transition_preserves_started_at(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Planning", started=STARTED_ISO)
            result = await runs.transition(RUN_ID, RunStatus.FINALIZING)
            assert result.started_at == datetime.fromisoformat(STARTED_ISO)
        finally:
            await db.close()

    _run(scenario())


def test_transition_created_to_stop_no_started_at(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Created")
            result = await runs.transition(RUN_ID, RunStatus.STOP_REQUESTED)
            assert result.started_at is None
        finally:
            await db.close()

    _run(scenario())


def test_transition_created_to_failed_no_started_at(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Created")
            result = await runs.transition(RUN_ID, RunStatus.FAILED)
            assert result.started_at is None
            assert result.finished_at is not None
        finally:
            await db.close()

    _run(scenario())


def test_transition_completed_sets_final_response_and_finished(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Finalizing", started=STARTED_ISO)
            result = await runs.transition(
                RUN_ID, RunStatus.COMPLETED, final_response="done"
            )
            assert result.status is RunStatus.COMPLETED
            assert result.final_response == "done"
            assert result.finished_at is not None
            assert result.started_at == datetime.fromisoformat(STARTED_ISO)
            assert (await runs.get(RUN_ID)).final_response == "done"
        finally:
            await db.close()

    _run(scenario())


def test_transition_final_response_none_keeps_existing(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(
                db, session.session_id, "Finalizing", started=STARTED_ISO, final="prior"
            )
            # No final_response argument -> existing value preserved.
            result = await runs.transition(RUN_ID, RunStatus.STOP_REQUESTED)
            assert result.final_response == "prior"
        finally:
            await db.close()

    _run(scenario())


def test_transition_clears_active_slot_on_terminal(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Finalizing", started=STARTED_ISO)
            await _set_active_run(db, RUN_ID)
            assert await runs.active_run_id() == RUN_ID
            await runs.transition(RUN_ID, RunStatus.COMPLETED, final_response="done")
            assert await runs.active_run_id() is None
        finally:
            await db.close()

    _run(scenario())


def test_transition_terminal_does_not_clear_other_run_slot(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            other = f"run_{'7' * 32}"
            await _seed_run(db, session.session_id, "Finalizing", started=STARTED_ISO)
            await _seed_run(db, session.session_id, "Created", run_id=other)
            await _set_active_run(db, other)
            await runs.transition(RUN_ID, RunStatus.COMPLETED, final_response="done")
            assert await runs.active_run_id() == other
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.CREATED, RunStatus.PLANNING),
        (RunStatus.PLANNING, RunStatus.CREATED),
        (RunStatus.STOP_REQUESTED, RunStatus.FAILED),
        (RunStatus.RETRYING, RunStatus.COMPLETED),
        (RunStatus.FINALIZING, RunStatus.PLANNING),
    ],
)
def test_transition_rejects_invalid_edge(
    db_path: Path, current: RunStatus, target: RunStatus
) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, current.value)
            with pytest.raises(AgentException) as exc:
                await runs.transition(RUN_ID, target)
            assert exc.value.error.code == "runtime.invalid_run_transition"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert f"{current.value} -> {target.value}" in exc.value.error.message_for_user
            assert (await runs.get(RUN_ID)).status is current
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    "terminal", [RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED]
)
@pytest.mark.parametrize("target", [RunStatus.PLANNING, RunStatus.FINALIZING])
def test_transition_terminal_cannot_transition(
    db_path: Path, terminal: RunStatus, target: RunStatus
) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, terminal.value, started=STARTED_ISO, finished=NOW_ISO)
            with pytest.raises(AgentException) as exc:
                await runs.transition(RUN_ID, target)
            assert exc.value.error.code == "runtime.invalid_run_transition"
            assert (await runs.get(RUN_ID)).status is terminal
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_target", ["Planning", None, 1, SessionStatus.ACTIVE])
def test_transition_rejects_non_exact_target(
    db_path: Path, bad_target: object
) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Created")
            with pytest.raises(TypeError):
                await runs.transition(RUN_ID, bad_target)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_transition_rejects_oversized_final_response(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Finalizing", started=STARTED_ISO)
            with pytest.raises(AgentException) as exc:
                await runs.transition(
                    RUN_ID,
                    RunStatus.COMPLETED,
                    final_response="a" * (MAX_FINAL_RESPONSE_BYTES + 1),
                )
            assert exc.value.error.code == "validation.payload_too_large"
            assert (await runs.get(RUN_ID)).status is RunStatus.FINALIZING
        finally:
            await db.close()

    _run(scenario())


def test_transition_rejects_non_string_final_response(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Finalizing", started=STARTED_ISO)
            with pytest.raises(TypeError):
                await runs.transition(RUN_ID, RunStatus.COMPLETED, final_response=123)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_transition_missing_run_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _sessions, runs = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await runs.transition(MISSING_RUN, RunStatus.PLANNING)
            assert exc.value.error.code == "runtime.run_not_found"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 15. fail
# --------------------------------------------------------------------------

def test_fail_persists_error_and_clears_slot(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            error = AgentError(
                "runtime.test_failure",
                ErrorCategory.INTERNAL_INVARIANT,
                "test failure",
            )
            failed = await runs.fail(run.run_id, error)
            assert failed.status is RunStatus.FAILED
            assert failed.finished_at is not None
            assert failed.to_dict()["failure_json"] == error.to_dict()
            assert await runs.active_run_id() is None
            row = await db.fetchone("SELECT failure_json FROM runs WHERE run_id = ?", (run.run_id,))
            assert row["failure_json"] == canonical_json_dumps(error.to_dict())
        finally:
            await db.close()

    _run(scenario())


def test_fail_failure_json_is_deeply_immutable(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            error = AgentError(
                "runtime.test_failure",
                ErrorCategory.INTERNAL_INVARIANT,
                "boom",
            )
            failed = await runs.fail(run.run_id, error)
            assert failed.failure_json is not None
            with pytest.raises(TypeError):
                failed.failure_json["code"] = "x"
        finally:
            await db.close()

    _run(scenario())


def test_fail_rejects_non_agent_error(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            with pytest.raises(TypeError):
                await runs.fail(run.run_id, "not an error")  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_fail_terminal_run_is_invalid_transition(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(
                db, session.session_id, "Completed", started=STARTED_ISO, finished=NOW_ISO
            )
            with pytest.raises(AgentException) as exc:
                await runs.fail(
                    RUN_ID,
                    AgentError("runtime.x", ErrorCategory.INTERNAL_INVARIANT, "x"),
                )
            assert exc.value.error.code == "runtime.invalid_run_transition"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 16/17. reconcile_interrupted
# --------------------------------------------------------------------------

NONTERMINAL_STATUSES = [
    "Created",
    "PreparingContext",
    "Planning",
    "Finalizing",
    "StopRequested",
    "Stopping",
    "Retrying",
]
TERMINAL_STATUSES = ["Completed", "Cancelled", "Failed"]


def test_reconcile_fails_nonterminal_preserves_terminal(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            for i, status in enumerate(NONTERMINAL_STATUSES):
                await _seed_run(
                    db, session.session_id, status, run_id=f"run_{i:032d}", started=STARTED_ISO
                )
            for i, status in enumerate(TERMINAL_STATUSES):
                await _seed_run(
                    db,
                    session.session_id,
                    status,
                    run_id=f"run_{(i + 9):032d}",
                    started=STARTED_ISO,
                    finished=NOW_ISO,
                )
            await _set_active_run(db, f"run_{0:032d}")
            changed = await runs.reconcile_interrupted()
            assert type(changed) is tuple
            assert len(changed) == len(NONTERMINAL_STATUSES)
            assert all(r.status is RunStatus.FAILED for r in changed)
            assert all(r.failure_json is not None for r in changed)
            assert all(r.failure_json["code"] == "runtime.interrupted" for r in changed)
            assert all(r.finished_at is not None for r in changed)
            # terminal runs untouched
            for i in range(len(TERMINAL_STATUSES)):
                terminal = await runs.get(f"run_{(i + 9):032d}")
                assert terminal.status.value == TERMINAL_STATUSES[i]
                assert terminal.failure_json is None
            assert await runs.active_run_id() is None
        finally:
            await db.close()

    _run(scenario())


def test_reconcile_is_idempotent(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Planning", run_id=f"run_{1:032d}", started=STARTED_ISO)
            first = await runs.reconcile_interrupted()
            assert len(first) == 1
            finished_at = first[0].finished_at
            failure = first[0].failure_json
            second = await runs.reconcile_interrupted()
            assert second == ()
            reloaded = await runs.get(f"run_{1:032d}")
            assert reloaded.finished_at == finished_at
            assert reloaded.failure_json == failure
        finally:
            await db.close()

    _run(scenario())


def test_reconcile_interrupted_error_category(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Planning", run_id=f"run_{1:032d}", started=STARTED_ISO)
            changed = await runs.reconcile_interrupted()
            assert changed[0].failure_json["category"] == ErrorCategory.INTERNAL_INVARIANT.value
            assert "stopped" in changed[0].failure_json["message_for_user"]
        finally:
            await db.close()

    _run(scenario())


def test_reconcile_clears_stale_active_slot_with_no_nonterminal(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(
                db, session.session_id, "Completed", run_id=f"run_{1:032d}",
                started=STARTED_ISO, finished=NOW_ISO,
            )
            await _set_active_run(db, f"run_{1:032d}")
            changed = await runs.reconcile_interrupted()
            assert changed == ()
            assert await runs.active_run_id() is None
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 18. transaction-local return snapshots
# --------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["create", "transition", "fail"])
def test_mutation_returns_transaction_local_snapshot(
    db_path: Path, op: str
) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            target_id = f"run_{'1' * 32}"
            if op != "create":
                await _seed_run(
                    db, session.session_id, "Planning", run_id=target_id, started=STARTED_ISO
                )
                await _set_active_run(db, target_id)

            original_write_transaction = db.write_transaction
            mutation_committed = asyncio.Event()
            allow_return = asyncio.Event()
            paused = False

            @asynccontextmanager
            async def pause_after_commit():
                nonlocal paused
                async with original_write_transaction() as conn:
                    yield conn
                if not paused:
                    paused = True
                    mutation_committed.set()
                    await allow_return.wait()

            db.write_transaction = pause_after_commit

            if op == "create":
                task = asyncio.create_task(
                    runs.create_and_acquire(session.session_id, "race", {"m": 1})
                )
            elif op == "transition":
                task = asyncio.create_task(
                    runs.transition(target_id, RunStatus.FINALIZING)
                )
            else:
                task = asyncio.create_task(
                    runs.fail(
                        target_id,
                        AgentError("runtime.x", ErrorCategory.INTERNAL_INVARIANT, "x"),
                    )
                )

            await mutation_committed.wait()

            if op == "create":
                row = await db.fetchone(
                    "SELECT run_id FROM runs WHERE user_input = 'race'"
                )
                target_id = row["run_id"]

            assert await _count(db, "runs", "run_id = ?", (target_id,)) == 1

            # Raw-delete the committed run during the pause, bypassing the
            # repository active-run guard. A post-commit re-read would now fail.
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM runs WHERE run_id = ?", (target_id,))
            assert await _count(db, "runs", "run_id = ?", (target_id,)) == 0

            allow_return.set()
            result = await task

            assert result.run_id == target_id
            if op == "create":
                assert result.status is RunStatus.CREATED
            elif op == "transition":
                assert result.status is RunStatus.FINALIZING
            else:
                assert result.status is RunStatus.FAILED
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 19. no events / no session sequence changes
# --------------------------------------------------------------------------

def test_repository_writes_no_events_and_keeps_session_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            await runs.transition(run.run_id, RunStatus.PREPARING_CONTEXT)
            await runs.fail(
                run.run_id,
                AgentError("runtime.x", ErrorCategory.INTERNAL_INVARIANT, "x"),
            )
            await runs.reconcile_interrupted()
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
# capture model_snapshot before the first await (no mutable-input race)
# --------------------------------------------------------------------------

def test_create_captures_model_snapshot_before_first_await(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs = await _open(db_path)
        try:
            session = await sessions.create("A")

            original_write_transaction = db.write_transaction
            holder_entered = asyncio.Event()
            release_holder = asyncio.Event()
            entered_write_transaction = asyncio.Event()

            async def hold_write_lock() -> None:
                async with original_write_transaction():
                    holder_entered.set()
                    await release_holder.wait()

            @asynccontextmanager
            async def observed_write_transaction():
                # Set before acquiring the lock: by the time write_transaction is
                # called, create_and_acquire has finished all validation and
                # canonicalization, so snapshot_text is already captured.
                entered_write_transaction.set()
                async with original_write_transaction() as conn:
                    yield conn

            holder = asyncio.create_task(hold_write_lock())
            await holder_entered.wait()
            db.write_transaction = observed_write_transaction

            source = {"model": {"name": "original", "options": [1, 2]}}

            create_task = asyncio.create_task(
                runs.create_and_acquire(session.session_id, "inspect", source)
            )
            await entered_write_transaction.wait()

            # Mutate the caller-owned dict while create waits for the write lock.
            source["model"]["name"] = "mutated-while-waiting"
            source["model"]["options"].append(3)
            source["new"] = True

            release_holder.set()
            await holder
            record = await create_task

            row = await db.fetchone(
                "SELECT model_snapshot_json FROM runs WHERE run_id = ?",
                (record.run_id,),
            )
            assert row["model_snapshot_json"] == (
                '{"model":{"name":"original","options":[1,2]}}'
            )
            assert record.to_dict()["model_snapshot_json"] == {
                "model": {"name": "original", "options": [1, 2]}
            }
            loaded = await runs.get(record.run_id)
            assert loaded.to_dict()["model_snapshot_json"] == (
                record.to_dict()["model_snapshot_json"]
            )

            # A later mutation of the caller dict has no effect.
            source["model"]["name"] = "again"
            assert record.to_dict()["model_snapshot_json"] == {
                "model": {"name": "original", "options": [1, 2]}
            }
        finally:
            await db.close()

    _run(scenario())
