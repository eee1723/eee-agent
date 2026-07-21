from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.core import AgentException, ErrorCategory
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.events import EventStore, ReplayResult, SessionSnapshotData
from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunStatus,
    SessionStatus,
    canonical_json_dumps,
)
from eee_agent.runtime.runs import RunRepository
from eee_agent.runtime.sessions import SessionRepository

RUN_ID = f"run_{'0' * 32}"
EVENT_ID = f"evt_{'0' * 32}"
MISSING_SESSION = f"ses_{'a' * 32}"
MISSING_RUN = f"run_{'b' * 32}"
NOW_ISO = "2026-07-14T02:30:00+00:00"
OLD_ISO = "2026-01-01T00:00:00+00:00"
STARTED_ISO = "2026-07-14T03:00:00+00:00"
CUTOFF = datetime(2026, 7, 1, tzinfo=timezone.utc)
MAX_PAYLOAD_BYTES = 262_144

_RUN_INSERT = (
    "INSERT INTO runs(run_id, session_id, status, user_input, final_response, "
    "created_at, started_at, finished_at, failure_json, model_snapshot_json) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_EVENT_INSERT = (
    "INSERT INTO events(event_id, session_id, run_id, seq, event_type, timestamp, "
    "payload_json, retention_class, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)"
)


def _run(coro):
    return asyncio.run(coro)


async def _open(db_path: Path):
    db = await RuntimeDatabase.open(db_path)
    return db, SessionRepository(db), RunRepository(db), EventStore(db)


async def _seed_run(
    db: RuntimeDatabase,
    session_id: str,
    status: str,
    run_id: str = RUN_ID,
    created_at: str = NOW_ISO,
    started: str | None = None,
    finished: str | None = None,
    model: str = '{"m":1}',
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            _RUN_INSERT,
            (run_id, session_id, status, "in", None, created_at, started, finished, None, model),
        )


async def _seed_event(
    db: RuntimeDatabase,
    session_id: str,
    run_id: str | None,
    seq: int,
    retention: str = "operational",
    timestamp: str = OLD_ISO,
    payload: str = '{"i":1}',
    event_type: str = "run.test",
    event_id: str | None = None,
) -> None:
    eid = event_id or f"evt_{seq:032x}"
    async with db.write_transaction() as conn:
        await conn.execute(
            _EVENT_INSERT,
            (eid, session_id, run_id, seq, event_type, timestamp, payload, retention),
        )


async def _set_bounds(
    db: RuntimeDatabase, session_id: str, last_seq: int, floor: int = 0
) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "UPDATE sessions SET last_seq = ?, replay_floor_seq = ? WHERE session_id = ?",
            (last_seq, floor, session_id),
        )


async def _set_active_run(db: RuntimeDatabase, run_id: str) -> None:
    async with db.write_transaction() as conn:
        await conn.execute(
            "UPDATE runtime_state SET active_run_id = ?, updated_at = ? WHERE singleton_id = 1",
            (run_id, NOW_ISO),
        )


async def _count(db: RuntimeDatabase, where: str = "", params: tuple = ()) -> int:
    return (await db.fetchone(f"SELECT COUNT(*) AS c FROM events{' WHERE ' + where if where else ''}", params))["c"]


async def _last_seq(db: RuntimeDatabase, session_id: str) -> int:
    return (await db.fetchone("SELECT last_seq FROM sessions WHERE session_id = ?", (session_id,)))["last_seq"]


async def _floor(db: RuntimeDatabase, session_id: str) -> int:
    return (await db.fetchone("SELECT replay_floor_seq FROM sessions WHERE session_id = ?", (session_id,)))["replay_floor_seq"]


# --------------------------------------------------------------------------
# 1/2. append basics + DomainEvent reuse
# --------------------------------------------------------------------------

def test_append_returns_immutable_event_and_increments_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            event = await store.append(
                session_id=session.session_id,
                run_id=None,
                event_type="session.test",
                payload={"value": 1, "nested": {"x": [1, 2]}},
                retention_class=RetentionClass.DURABLE,
            )
            assert type(event) is EventRecord
            assert event.event_id.startswith("evt_")
            assert event.session_id == session.session_id
            assert event.run_id is None
            assert event.seq == 1
            assert event.event_type == "session.test"
            assert event.timestamp.tzinfo is not None
            assert event.retention_class is RetentionClass.DURABLE
            assert event.schema_version == 1
            assert event.to_dict()["payload"] == {"value": 1, "nested": {"x": [1, 2]}}
            with pytest.raises(TypeError):
                event.payload["new"] = True
            assert await _last_seq(db, session.session_id) == 1
            assert await _floor(db, session.session_id) == 0
            row = await db.fetchone("SELECT payload_json FROM events WHERE event_id = ?", (event.event_id,))
            assert row["payload_json"] == '{"nested":{"x":[1,2]},"value":1}'
        finally:
            await db.close()

    _run(scenario())


def test_append_does_not_change_session_metadata(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            before = await sessions.get(session.session_id)
            await store.append(
                session_id=session.session_id,
                run_id=None,
                event_type="session.test",
                payload={},
                retention_class=RetentionClass.DURABLE,
            )
            after = await sessions.get(session.session_id)
            assert after.title == before.title
            assert after.status is SessionStatus.ACTIVE
            assert after.updated_at == before.updated_at
            assert after.replay_floor_seq == before.replay_floor_seq
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize(
    "payload", [{1: "v"}, {"x": float("nan")}, {"x": float("inf")}, {"x": (1, 2)}, {"x": {1, 2}}],
    ids=["int-key", "nan", "inf", "tuple", "set"],
)
def test_append_rejects_non_json_payload(db_path: Path, payload: object) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises((TypeError, ValueError)):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload=payload,  # type: ignore[arg-type]
                    retention_class=RetentionClass.DURABLE,
                )
            assert await _last_seq(db, session.session_id) == 0
            assert await _count(db) == 0
        finally:
            await db.close()

    _run(scenario())


def test_append_rejects_non_namespaced_event_type(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(ValueError, match="namespaced"):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="created", payload={},
                    retention_class=RetentionClass.DURABLE,
                )
            assert await _count(db) == 0
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 3. payload deep snapshot + pre-first-await race
# --------------------------------------------------------------------------

def test_append_snapshots_payload_input(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            source = {"model": {"name": "original"}}
            event = await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload=source,
                retention_class=RetentionClass.DURABLE,
            )
            source["model"]["name"] = "mutated"
            source["new"] = True
            assert event.to_dict()["payload"] == {"model": {"name": "original"}}
            row = await db.fetchone("SELECT payload_json FROM events WHERE event_id = ?", (event.event_id,))
            assert row["payload_json"] == '{"model":{"name":"original"}}'
        finally:
            await db.close()

    _run(scenario())


def test_append_captures_payload_before_first_await(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            original = db.write_transaction
            holder_entered = asyncio.Event()
            release_holder = asyncio.Event()
            entered = asyncio.Event()

            async def hold_write_lock() -> None:
                async with original():
                    holder_entered.set()
                    await release_holder.wait()

            @asynccontextmanager
            async def observed():
                entered.set()
                async with original() as conn:
                    yield conn

            holder = asyncio.create_task(hold_write_lock())
            await holder_entered.wait()
            db.write_transaction = observed

            source = {"model": {"name": "original", "options": [1, 2]}}
            append_task = asyncio.create_task(
                store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload=source,
                    retention_class=RetentionClass.DURABLE,
                )
            )
            await entered.wait()
            source["model"]["name"] = "mutated-while-waiting"
            source["model"]["options"].append(3)
            source["new"] = True

            release_holder.set()
            await holder
            event = await append_task

            row = await db.fetchone("SELECT payload_json FROM events WHERE event_id = ?", (event.event_id,))
            assert row["payload_json"] == '{"model":{"name":"original","options":[1,2]}}'
            assert event.to_dict()["payload"] == {"model": {"name": "original", "options": [1, 2]}}
            replayed = await store.replay(session.session_id, after_seq=0, limit=10)
            assert replayed.events[0].to_dict()["payload"] == {
                "model": {"name": "original", "options": [1, 2]}
            }
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 4. payload size (canonical UTF-8 bytes)
# --------------------------------------------------------------------------

def test_append_accepts_payload_at_byte_boundary(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            payload = {"k": "a" * (MAX_PAYLOAD_BYTES - 8)}
            assert len(canonical_json_dumps(payload).encode("utf-8")) == MAX_PAYLOAD_BYTES
            event = await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload=payload,
                retention_class=RetentionClass.DURABLE,
            )
            assert event.seq == 1
        finally:
            await db.close()

    _run(scenario())


def test_append_rejects_payload_one_byte_over(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            payload = {"k": "a" * (MAX_PAYLOAD_BYTES - 7)}
            assert len(canonical_json_dumps(payload).encode("utf-8")) == MAX_PAYLOAD_BYTES + 1
            with pytest.raises(AgentException) as exc:
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload=payload,
                    retention_class=RetentionClass.DURABLE,
                )
            assert exc.value.error.code == "validation.payload_too_large"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _last_seq(db, session.session_id) == 0
            assert await _count(db) == 0
        finally:
            await db.close()

    _run(scenario())


def test_append_payload_size_counts_utf8_bytes(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            # "é" is 2 UTF-8 bytes; 131072 code points -> 262144 value bytes + 7 overhead = 262151.
            with pytest.raises(AgentException) as exc:
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"k": "é" * 131072},
                    retention_class=RetentionClass.DURABLE,
                )
            assert exc.value.error.code == "validation.payload_too_large"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 5/6/7. session/run/retention validation
# --------------------------------------------------------------------------

def test_append_missing_session_does_not_consume_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, _sessions, _runs, store = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await store.append(
                    session_id=MISSING_SESSION, run_id=None,
                    event_type="session.test", payload={},
                    retention_class=RetentionClass.DURABLE,
                )
            assert exc.value.error.code == "runtime.session_not_found"
            assert await _count(db) == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_id", [RUN_ID, EVENT_ID, "ses_short", "x"])
def test_append_rejects_bad_session_id(db_path: Path, bad_id: str) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await store.append(
                    session_id=bad_id, run_id=None, event_type="session.test",
                    payload={}, retention_class=RetentionClass.DURABLE,
                )
        finally:
            await db.close()

    _run(scenario())


def test_append_run_relationship(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            await _seed_run(db, session.session_id, "Planning", run_id=RUN_ID, started=STARTED_ISO)
            # valid run in same session
            event = await store.append(
                session_id=session.session_id, run_id=RUN_ID,
                event_type="run.test", payload={},
                retention_class=RetentionClass.OPERATIONAL,
            )
            assert event.run_id == RUN_ID
            assert event.seq == 1
            # missing run
            with pytest.raises(AgentException) as exc:
                await store.append(
                    session_id=session.session_id, run_id=MISSING_RUN,
                    event_type="run.test", payload={},
                    retention_class=RetentionClass.OPERATIONAL,
                )
            assert exc.value.error.code == "runtime.run_not_found"
            assert await _last_seq(db, session.session_id) == 1  # not consumed
        finally:
            await db.close()

    _run(scenario())


def test_append_run_session_mismatch_rejected(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            a = await sessions.create("A")
            b = await sessions.create("B")
            await _seed_run(db, b.session_id, "Planning", run_id=RUN_ID, started=STARTED_ISO)
            with pytest.raises(AgentException) as exc:
                await store.append(
                    session_id=a.session_id, run_id=RUN_ID,
                    event_type="run.test", payload={},
                    retention_class=RetentionClass.OPERATIONAL,
                )
            assert exc.value.error.code == "runtime.run_session_mismatch"
            assert exc.value.error.category is ErrorCategory.VALIDATION
            assert await _last_seq(db, a.session_id) == 0
            assert await _count(db) == 0
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_run_id", [f"ses_{'0' * 32}", EVENT_ID, "run_short"])
def test_append_rejects_bad_run_id(db_path: Path, bad_run_id: str) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(ValueError):
                await store.append(
                    session_id=session.session_id, run_id=bad_run_id,
                    event_type="run.test", payload={},
                    retention_class=RetentionClass.OPERATIONAL,
                )
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad_run_id", [123, ["list"]])
def test_append_rejects_non_string_run_id(db_path: Path, bad_run_id: object) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(TypeError):
                await store.append(
                    session_id=session.session_id, run_id=bad_run_id,  # type: ignore[arg-type]
                    event_type="run.test", payload={},
                    retention_class=RetentionClass.OPERATIONAL,
                )
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad", ["durable", "operational", None, 1, SessionStatus.ACTIVE])
def test_append_rejects_non_exact_retention(db_path: Path, bad: object) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            with pytest.raises(TypeError):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={},
                    retention_class=bad,  # type: ignore[arg-type]
                )
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 8. 50 concurrent appends
# --------------------------------------------------------------------------

def test_fifty_concurrent_appends_no_duplicates_or_gaps(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            events = await asyncio.gather(*(
                store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"index": index},
                    retention_class=RetentionClass.DURABLE,
                )
                for index in range(50)
            ))
            assert sorted(e.seq for e in events) == list(range(1, 51))
            assert len({e.seq for e in events}) == 50
            assert await _last_seq(db, session.session_id) == 50
            assert await _count(db) == 50
            replayed = await store.replay(session.session_id, after_seq=0, limit=100)
            assert [e.seq for e in replayed.events] == list(range(1, 51))
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 9. append atomic rollback (INSERT failure does not consume seq)
# --------------------------------------------------------------------------

def test_append_insert_failure_rolls_back_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            async with db.write_transaction() as conn:
                await conn.execute(
                    "CREATE TRIGGER fail_event_insert BEFORE INSERT ON events "
                    "BEGIN SELECT RAISE(ABORT, 'forced event insert failure'); END"
                )
            with pytest.raises(sqlite3.Error):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={},
                    retention_class=RetentionClass.DURABLE,
                )
            assert await _last_seq(db, session.session_id) == 0
            assert await _floor(db, session.session_id) == 0
            assert await _count(db) == 0
            async with db.write_transaction() as conn:
                await conn.execute("DROP TRIGGER fail_event_insert")
            event = await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload={},
                retention_class=RetentionClass.DURABLE,
            )
            assert event.seq == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 10. append transaction-local return
# --------------------------------------------------------------------------

def test_append_returns_transaction_local_snapshot(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            original = db.write_transaction
            committed = asyncio.Event()
            allow_return = asyncio.Event()
            paused = False

            @asynccontextmanager
            async def pause_after_commit():
                nonlocal paused
                async with original() as conn:
                    yield conn
                if not paused:
                    paused = True
                    committed.set()
                    await allow_return.wait()

            db.write_transaction = pause_after_commit
            task = asyncio.create_task(
                store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"i": 1},
                    retention_class=RetentionClass.DURABLE,
                )
            )
            await committed.wait()
            eid = (await db.fetchone("SELECT event_id FROM events"))["event_id"]
            async with db.write_transaction() as conn:
                await conn.execute("DELETE FROM events WHERE event_id = ?", (eid,))
            allow_return.set()
            event = await task
            assert event.event_id == eid
            assert event.seq == 1
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 11/12/13/14. replay
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_replay_rejects_bad_after_seq(db_path: Path, bad: object) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises((TypeError, ValueError)):
                await store.replay(MISSING_SESSION, after_seq=bad, limit=10)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_replay_rejects_negative_after_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await store.replay(MISSING_SESSION, after_seq=-1, limit=10)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad", [True, 1.0, "1", None, 0, -1, 1001])
def test_replay_rejects_bad_limit(db_path: Path, bad: object) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises((TypeError, ValueError)):
                await store.replay(MISSING_SESSION, after_seq=0, limit=bad)  # type: ignore[arg-type]
        finally:
            await db.close()

    _run(scenario())


def test_replay_returns_ascending_within_limit(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            for i in range(5):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"i": i},
                    retention_class=RetentionClass.DURABLE,
                )
            result = await store.replay(session.session_id, after_seq=1, limit=2)
            assert type(result) is ReplayResult
            assert type(result.events) is tuple
            assert [e.seq for e in result.events] == [2, 3]
            assert result.last_seq == 5
            assert result.replay_floor_seq == 0
            assert result.snapshot_required is False
            # empty when after_seq >= last_seq
            empty = await store.replay(session.session_id, after_seq=5, limit=10)
            assert empty.events == ()
            beyond = await store.replay(session.session_id, after_seq=99, limit=10)
            assert beyond.events == ()
        finally:
            await db.close()

    _run(scenario())


def test_replay_missing_session_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await store.replay(MISSING_SESSION, after_seq=0, limit=10)
            assert exc.value.error.code == "runtime.session_not_found"
        finally:
            await db.close()

    _run(scenario())


def test_replay_snapshot_required_only_when_below_floor(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            for i in range(5):
                await store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"i": i},
                    retention_class=RetentionClass.DURABLE,
                )
            await _set_bounds(db, session.session_id, 5, floor=3)
            # after_seq < floor -> snapshot_required, start at floor
            below = await store.replay(session.session_id, after_seq=1, limit=10)
            assert below.snapshot_required is True
            assert [e.seq for e in below.events] == [4, 5]
            # after_seq == floor -> no snapshot, start after floor
            at = await store.replay(session.session_id, after_seq=3, limit=10)
            assert at.snapshot_required is False
            assert [e.seq for e in at.events] == [4, 5]
            # after_seq > floor -> no snapshot
            above = await store.replay(session.session_id, after_seq=4, limit=10)
            assert above.snapshot_required is False
            assert [e.seq for e in above.events] == [5]
            assert above.replay_floor_seq == 3
            assert above.last_seq == 5
        finally:
            await db.close()

    _run(scenario())


def test_replay_reads_consistent_bounds_under_concurrent_append(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload={"i": 0},
                retention_class=RetentionClass.DURABLE,
            )
            original = db.write_transaction
            replay_holding = asyncio.Event()
            allow_replay = asyncio.Event()

            @asynccontextmanager
            async def pause_inside():
                async with original() as conn:
                    replay_holding.set()
                    await allow_replay.wait()
                    yield conn

            db.write_transaction = pause_inside
            replay_task = asyncio.create_task(store.replay(session.session_id, after_seq=0, limit=10))
            await replay_holding.wait()

            append_task = asyncio.create_task(
                store.append(
                    session_id=session.session_id, run_id=None,
                    event_type="session.test", payload={"i": 1},
                    retention_class=RetentionClass.DURABLE,
                )
            )
            await asyncio.sleep(0.01)
            assert not append_task.done()  # blocked on the lock held by replay

            allow_replay.set()
            result = await replay_task
            assert result.last_seq == 1
            assert [e.seq for e in result.events] == [1]
            appended = await append_task
            assert appended.seq == 2
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 15/16/17/18. prune_operational
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_prune_rejects_bad_through_seq(db_path: Path, bad: object) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises((TypeError, ValueError)):
                await store.prune_operational(
                    MISSING_SESSION, through_seq=bad,  # type: ignore[arg-type]
                    older_than=CUTOFF,
                )
        finally:
            await db.close()

    _run(scenario())


def test_prune_rejects_negative_through_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await store.prune_operational(MISSING_SESSION, through_seq=-1, older_than=CUTOFF)
        finally:
            await db.close()

    _run(scenario())


@pytest.mark.parametrize("bad", [True, "x", None, 1])
def test_prune_rejects_bad_older_than(db_path: Path, bad: object) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises((TypeError, ValueError)):
                await store.prune_operational(
                    MISSING_SESSION, through_seq=0, older_than=bad,  # type: ignore[arg-type]
                )
        finally:
            await db.close()

    _run(scenario())


def test_prune_rejects_naive_older_than(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(ValueError):
                await store.prune_operational(
                    MISSING_SESSION, through_seq=0,
                    older_than=datetime(2026, 7, 1),
                )
        finally:
            await db.close()

    _run(scenario())


def test_prune_only_deletes_eligible_operational(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            term = f"run_{'1' * 32}"
            nonterm = f"run_{'2' * 32}"
            await _seed_run(db, session.session_id, "Completed", run_id=term, started=STARTED_ISO, finished=NOW_ISO)
            await _seed_run(db, session.session_id, "Planning", run_id=nonterm, started=STARTED_ISO)
            await _seed_event(db, session.session_id, term, seq=1, retention="operational", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, term, seq=2, retention="durable", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, nonterm, seq=3, retention="operational", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, None, seq=4, retention="operational", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, term, seq=5, retention="operational", timestamp=NOW_ISO)
            await _seed_event(db, session.session_id, term, seq=6, retention="operational", timestamp=OLD_ISO)
            await _set_bounds(db, session.session_id, 6, 0)

            deleted = await store.prune_operational(
                session.session_id, through_seq=5, older_than=CUTOFF
            )
            assert deleted == 1
            remaining = sorted(r["seq"] for r in await db.fetchall(
                "SELECT seq FROM events WHERE session_id = ?", (session.session_id,)
            ))
            assert remaining == [2, 3, 4, 5, 6]
            assert await _floor(db, session.session_id) == 1
        finally:
            await db.close()

    _run(scenario())


def test_prune_floor_uses_greatest_deleted_seq(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            term = f"run_{'1' * 32}"
            await _seed_run(db, session.session_id, "Completed", run_id=term, started=STARTED_ISO, finished=NOW_ISO)
            await _seed_event(db, session.session_id, term, seq=1, retention="operational", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, term, seq=2, retention="durable", timestamp=OLD_ISO)
            await _seed_event(db, session.session_id, term, seq=3, retention="operational", timestamp=OLD_ISO)
            await _set_bounds(db, session.session_id, 3, 0)
            deleted = await store.prune_operational(
                session.session_id, through_seq=10, older_than=CUTOFF
            )
            assert deleted == 2
            assert await _floor(db, session.session_id) == 3
            assert await _last_seq(db, session.session_id) == 3
        finally:
            await db.close()

    _run(scenario())


def test_prune_no_eligible_returns_zero_and_keeps_floor(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            term = f"run_{'1' * 32}"
            await _seed_run(db, session.session_id, "Completed", run_id=term, started=STARTED_ISO, finished=NOW_ISO)
            await _seed_event(db, session.session_id, term, seq=1, retention="durable", timestamp=OLD_ISO)
            await _set_bounds(db, session.session_id, 1, 0)
            deleted = await store.prune_operational(
                session.session_id, through_seq=10, older_than=CUTOFF
            )
            assert deleted == 0
            assert await _floor(db, session.session_id) == 0
            assert await _count(db) == 1
        finally:
            await db.close()

    _run(scenario())


def test_prune_is_idempotent(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            term = f"run_{'1' * 32}"
            await _seed_run(db, session.session_id, "Completed", run_id=term, started=STARTED_ISO, finished=NOW_ISO)
            await _seed_event(db, session.session_id, term, seq=1, retention="operational", timestamp=OLD_ISO)
            await _set_bounds(db, session.session_id, 1, 0)
            first = await store.prune_operational(session.session_id, through_seq=10, older_than=CUTOFF)
            floor_after_first = await _floor(db, session.session_id)
            assert first == 1
            second = await store.prune_operational(session.session_id, through_seq=10, older_than=CUTOFF)
            assert second == 0
            assert await _floor(db, session.session_id) == floor_after_first
        finally:
            await db.close()

    _run(scenario())


def test_prune_floor_update_atomic_rollback(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            term = f"run_{'1' * 32}"
            await _seed_run(db, session.session_id, "Completed", run_id=term, started=STARTED_ISO, finished=NOW_ISO)
            await _seed_event(db, session.session_id, term, seq=1, retention="operational", timestamp=OLD_ISO)
            await _set_bounds(db, session.session_id, 1, 0)
            async with db.write_transaction() as conn:
                await conn.execute(
                    "CREATE TRIGGER fail_floor BEFORE UPDATE OF replay_floor_seq ON sessions "
                    "BEGIN SELECT RAISE(ABORT, 'forced floor failure'); END"
                )
            with pytest.raises(sqlite3.Error):
                await store.prune_operational(session.session_id, through_seq=10, older_than=CUTOFF)
            assert await _count(db) == 1
            assert await _floor(db, session.session_id) == 0
            async with db.write_transaction() as conn:
                await conn.execute("DROP TRIGGER fail_floor")
            deleted = await store.prune_operational(session.session_id, through_seq=10, older_than=CUTOFF)
            assert deleted == 1
            assert await _count(db) == 0
            assert await _floor(db, session.session_id) == 1
        finally:
            await db.close()

    _run(scenario())


def test_prune_missing_session_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await store.prune_operational(MISSING_SESSION, through_seq=10, older_than=CUTOFF)
            assert exc.value.error.code == "runtime.session_not_found"
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 19/20/21/22. snapshot_data
# --------------------------------------------------------------------------

def test_snapshot_data_basic(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            await _seed_event(db, session.session_id, run.run_id, seq=1)
            await _set_bounds(db, session.session_id, 1)
            snap = await store.snapshot_data(session.session_id)
            assert type(snap) is SessionSnapshotData
            assert snap.session.session_id == session.session_id
            assert snap.snapshot_seq == 1
            assert snap.session.last_seq == 1
            assert type(snap.runs) is tuple
            assert snap.active_run is not None
            assert snap.active_run.run_id == run.run_id
            assert snap.has_earlier_runs is False
            assert snap.earliest_included_run_id == snap.runs[0].run_id
        finally:
            await db.close()

    _run(scenario())


def test_snapshot_data_empty_runs(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            snap = await store.snapshot_data(session.session_id)
            assert snap.runs == ()
            assert snap.has_earlier_runs is False
            assert snap.earliest_included_run_id is None
            assert snap.active_run is None
            assert snap.snapshot_seq == 0
        finally:
            await db.close()

    _run(scenario())


def test_snapshot_data_newest_100_and_pagination(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            for i in range(105):
                created = f"2026-07-14T{i // 60:02d}:{i % 60:02d}:00+00:00"
                await _seed_run(
                    db, session.session_id, "Completed",
                    run_id=f"run_{i:032d}", created_at=created,
                    started=STARTED_ISO, finished=NOW_ISO,
                )
            snap = await store.snapshot_data(session.session_id)
            assert len(snap.runs) == 100
            assert snap.has_earlier_runs is True
            assert snap.earliest_included_run_id == f"run_{5:032d}"
            assert [r.run_id for r in snap.runs] == [f"run_{i:032d}" for i in range(5, 105)]
            assert len({r.run_id for r in snap.runs}) == 100
        finally:
            await db.close()

    _run(scenario())


def test_snapshot_data_includes_active_run_outside_newest_100(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            for i in range(101):
                created = f"2026-07-14T{i // 60:02d}:{i % 60:02d}:00+00:00"
                await _seed_run(
                    db, session.session_id, "Completed",
                    run_id=f"run_{i:032d}", created_at=created,
                    started=STARTED_ISO, finished=NOW_ISO,
                )
            await _set_active_run(db, f"run_{0:032d}")  # oldest, outside newest 100
            snap = await store.snapshot_data(session.session_id)
            assert len(snap.runs) == 100
            assert snap.has_earlier_runs is True
            assert snap.active_run is not None
            assert snap.active_run.run_id == f"run_{0:032d}"
        finally:
            await db.close()

    _run(scenario())


def test_snapshot_data_missing_session_raises(db_path: Path) -> None:
    async def scenario() -> None:
        db, _s, _r, store = await _open(db_path)
        try:
            with pytest.raises(AgentException) as exc:
                await store.snapshot_data(MISSING_SESSION)
            assert exc.value.error.code == "runtime.session_not_found"
        finally:
            await db.close()

    _run(scenario())


def test_snapshot_seq_precedes_concurrent_append(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, _runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload={"i": 0},
                retention_class=RetentionClass.DURABLE,
            )
            original = db.write_transaction
            committed = asyncio.Event()
            allow_return = asyncio.Event()
            paused = False

            @asynccontextmanager
            async def pause_after_commit():
                nonlocal paused
                async with original() as conn:
                    yield conn
                if not paused:
                    paused = True
                    committed.set()
                    await allow_return.wait()

            db.write_transaction = pause_after_commit
            snap_task = asyncio.create_task(store.snapshot_data(session.session_id))
            await committed.wait()
            appended = await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload={"i": 1},
                retention_class=RetentionClass.DURABLE,
            )
            allow_return.set()
            snap = await snap_task
            assert snap.snapshot_seq == 1
            assert snap.session.last_seq == 1
            assert appended.seq == 2  # strictly greater than snapshot_seq
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# 23/24. invariants: no events/seq side-effects from failures; no broadcast
# --------------------------------------------------------------------------

def test_failures_keep_session_bounds_and_event_count(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs, store = await _open(db_path)
        try:
            session = await sessions.create("A")
            run = await runs.create_and_acquire(session.session_id, "in", {"m": 1})
            ok = await store.append(
                session_id=session.session_id, run_id=run.run_id,
                event_type="run.test", payload={},
                retention_class=RetentionClass.OPERATIONAL,
            )
            assert ok.seq == 1
            # a failing append must not change bounds/events
            with pytest.raises(AgentException):
                await store.append(
                    session_id=session.session_id, run_id=MISSING_RUN,
                    event_type="run.test", payload={},
                    retention_class=RetentionClass.OPERATIONAL,
                )
            assert await _last_seq(db, session.session_id) == 1
            assert await _count(db) == 1
            # store still works afterwards
            again = await store.append(
                session_id=session.session_id, run_id=None,
                event_type="session.test", payload={},
                retention_class=RetentionClass.DURABLE,
            )
            assert again.seq == 2
        finally:
            await db.close()

    _run(scenario())


# --------------------------------------------------------------------------
# cross-session active-run scoping + exact public type hints
# --------------------------------------------------------------------------

def test_snapshot_does_not_include_other_sessions_active_run(db_path: Path) -> None:
    async def scenario() -> None:
        db, sessions, runs, store = await _open(db_path)
        try:
            a = await sessions.create("A")
            b = await sessions.create("B")
            active_a = await runs.create_and_acquire(a.session_id, "inspect", {"m": 1})

            snapshot_b = await store.snapshot_data(b.session_id)
            assert snapshot_b.session.session_id == b.session_id
            assert snapshot_b.active_run is None
            assert snapshot_b.runs == ()

            snapshot_a = await store.snapshot_data(a.session_id)
            assert snapshot_a.session.session_id == a.session_id
            assert snapshot_a.active_run is not None
            assert snapshot_a.active_run.run_id == active_a.run_id
            assert snapshot_a.active_run.session_id == a.session_id

            # B's snapshot must carry none of A's run data.
            assert active_a.run_id not in {r.run_id for r in snapshot_b.runs}
            assert "inspect" not in {r.user_input for r in snapshot_b.runs}
        finally:
            await db.close()

    _run(scenario())


def test_session_snapshot_data_has_exact_public_type_hints() -> None:
    from typing import get_type_hints

    from eee_agent.runtime.models import RunRecord, SessionRecord

    hints = get_type_hints(SessionSnapshotData)
    assert hints["session"] is SessionRecord
    assert hints["runs"] == tuple[RunRecord, ...]
    assert hints["active_run"] == RunRecord | None
    assert hints["snapshot_seq"] is int
    assert hints["has_earlier_runs"] is bool
    assert hints["earliest_included_run_id"] == str | None
