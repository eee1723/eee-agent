from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from eee_agent.core import AgentError, AgentException, ErrorCategory, IdKind, require_id
from eee_agent.core.events import DomainEvent, JsonValue
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionRecord,
    canonical_json_dumps,
    canonical_json_loads,
)
from eee_agent.runtime.runs import _RUN_COLUMNS, _row_to_run
from eee_agent.runtime.sessions import _SESSION_COLUMNS, _row_to_session

_MAX_PAYLOAD_BYTES = 262_144
_MAX_REPLAY_LIMIT = 1000
_TERMINAL_RUN_VALUES = (
    RunStatus.COMPLETED.value,
    RunStatus.CANCELLED.value,
    RunStatus.FAILED.value,
)
_TERMINAL_PLACEHOLDERS = ",".join("?" for _ in _TERMINAL_RUN_VALUES)

_EVENT_COLUMNS = (
    "event_id, session_id, run_id, seq, event_type, timestamp, payload_json, "
    "retention_class, schema_version"
)

# Prune eligibility predicate, shared by the count/max read and the DELETE so
# the two cannot drift. An operational event is deletable only when its run
# belongs to the same session and is terminal, and the event is old enough.
_PRUNE_ELIGIBLE = (
    "session_id = ? AND retention_class = 'operational' AND seq <= ? "
    "AND run_id IS NOT NULL AND timestamp < ? "
    "AND run_id IN (SELECT run_id FROM runs WHERE session_id = ? "
    f"AND status IN ({_TERMINAL_PLACEHOLDERS}))"
)


def _require_session_id(session_id: object) -> str:
    if type(session_id) is not str:
        raise TypeError("session_id must be a string")
    return require_id(session_id, IdKind.SESSION)


def _require_optional_run_id(run_id: object) -> str | None:
    if run_id is None:
        return None
    if type(run_id) is not str:
        raise TypeError("run_id must be a string or None")
    return require_id(run_id, IdKind.RUN)


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _require_aware_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _session_not_found() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.session_not_found",
            category=ErrorCategory.VALIDATION,
            message_for_user="The Runtime session does not exist.",
        )
    )


def _run_not_found() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.run_not_found",
            category=ErrorCategory.VALIDATION,
            message_for_user="The Runtime run does not exist.",
        )
    )


def _run_session_mismatch() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.run_session_mismatch",
            category=ErrorCategory.VALIDATION,
            message_for_user="The run does not belong to this session.",
        )
    )


def _payload_too_large() -> AgentException:
    return AgentException(
        AgentError(
            code="validation.payload_too_large",
            category=ErrorCategory.VALIDATION,
            message_for_user="Event payload exceeds the maximum allowed size.",
        )
    )


def _row_to_event(row) -> EventRecord:
    return EventRecord(
        event_id=row["event_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        seq=row["seq"],
        event_type=row["event_type"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        payload=canonical_json_loads(row["payload_json"]),
        retention_class=RetentionClass(row["retention_class"]),
        schema_version=row["schema_version"],
    )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    events: tuple[EventRecord, ...]
    replay_floor_seq: int
    last_seq: int
    snapshot_required: bool


@dataclass(frozen=True, slots=True)
class SessionSnapshotData:
    session: SessionRecord
    runs: tuple[RunRecord, ...]
    active_run: RunRecord | None
    snapshot_seq: int
    has_earlier_runs: bool
    earliest_included_run_id: str | None


class EventStore:
    """Atomic event append, replay, retention, and snapshot data.

    Append allocates a per-session sequence and inserts the event in one
    transaction so rollback leaves ``last_seq`` and the events table unchanged.
    The store never broadcasts and never touches checkpoints; the service layer
    broadcasts after ``append`` returns.
    """

    def __init__(self, database: RuntimeDatabase) -> None:
        self._database = database

    async def append(
        self,
        *,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict[str, JsonValue],
        retention_class: RetentionClass,
    ) -> EventRecord:
        sid = _require_session_id(session_id)
        rid = _require_optional_run_id(run_id)
        if type(retention_class) is not RetentionClass:
            raise TypeError("retention_class must be an exact RetentionClass")
        # Foundation DomainEvent validates the namespaced event type and the
        # strict payload, and freezes the payload. Capture an independent
        # snapshot from it before the first await so a caller mutating the
        # original dict while we wait for the write lock cannot diverge the DB
        # text and the returned record.
        domain = DomainEvent.create(event_type=event_type, payload=payload)
        payload_value = domain.to_dict()["payload"]
        payload_text = canonical_json_dumps(payload_value)
        if len(payload_text.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise _payload_too_large()
        timestamp = domain.timestamp
        timestamp_iso = timestamp.isoformat()

        async with self._database.write_transaction() as conn:
            sess_cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
            )
            if await sess_cursor.fetchone() is None:
                raise _session_not_found()
            if rid is not None:
                run_cursor = await conn.execute(
                    "SELECT session_id FROM runs WHERE run_id = ?", (rid,)
                )
                run_row = await run_cursor.fetchone()
                if run_row is None:
                    raise _run_not_found()
                if run_row["session_id"] != sid:
                    raise _run_session_mismatch()
            await conn.execute(
                "UPDATE sessions SET last_seq = last_seq + 1 WHERE session_id = ?",
                (sid,),
            )
            seq_cursor = await conn.execute(
                "SELECT last_seq FROM sessions WHERE session_id = ?", (sid,)
            )
            seq = (await seq_cursor.fetchone())["last_seq"]
            await conn.execute(
                f"INSERT INTO events({_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    domain.event_id,
                    sid,
                    rid,
                    seq,
                    domain.event_type,
                    timestamp_iso,
                    payload_text,
                    retention_class.value,
                    domain.schema_version,
                ),
            )
            record = EventRecord(
                event_id=domain.event_id,
                session_id=sid,
                run_id=rid,
                seq=seq,
                event_type=domain.event_type,
                timestamp=timestamp,
                payload=payload_value,
                retention_class=retention_class,
                schema_version=domain.schema_version,
            )
        return record

    async def replay(
        self,
        session_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> ReplayResult:
        _require_int(after_seq, "after_seq")
        if after_seq < 0:
            raise ValueError("after_seq must be >= 0")
        _require_int(limit, "limit")
        if limit < 1 or limit > _MAX_REPLAY_LIMIT:
            raise ValueError("limit must be between 1 and 1000")
        sid = _require_session_id(session_id)
        async with self._database.write_transaction() as conn:
            sess_cursor = await conn.execute(
                "SELECT last_seq, replay_floor_seq FROM sessions WHERE session_id = ?",
                (sid,),
            )
            sess_row = await sess_cursor.fetchone()
            if sess_row is None:
                raise _session_not_found()
            last_seq = sess_row["last_seq"]
            replay_floor_seq = sess_row["replay_floor_seq"]
            snapshot_required = after_seq < replay_floor_seq
            start_seq = replay_floor_seq if snapshot_required else after_seq
            ev_cursor = await conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (sid, start_seq, limit),
            )
            rows = await ev_cursor.fetchall()
            events = tuple(_row_to_event(row) for row in rows)
        return ReplayResult(
            events=events,
            replay_floor_seq=replay_floor_seq,
            last_seq=last_seq,
            snapshot_required=snapshot_required,
        )

    async def prune_operational(
        self,
        session_id: str,
        *,
        through_seq: int,
        older_than: datetime,
    ) -> int:
        _require_int(through_seq, "through_seq")
        if through_seq < 0:
            raise ValueError("through_seq must be >= 0")
        cutoff_iso = _require_aware_datetime(older_than, "older_than").isoformat()
        sid = _require_session_id(session_id)
        params = (sid, through_seq, cutoff_iso, sid, *_TERMINAL_RUN_VALUES)
        async with self._database.write_transaction() as conn:
            sess_cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
            )
            if await sess_cursor.fetchone() is None:
                raise _session_not_found()
            elig_cursor = await conn.execute(
                f"SELECT seq FROM events WHERE {_PRUNE_ELIGIBLE}", params
            )
            elig_rows = await elig_cursor.fetchall()
            if not elig_rows:
                return 0
            greatest = max(row["seq"] for row in elig_rows)
            await conn.execute(
                f"DELETE FROM events WHERE {_PRUNE_ELIGIBLE}", params
            )
            await conn.execute(
                "UPDATE sessions SET replay_floor_seq = MAX(replay_floor_seq, ?) "
                "WHERE session_id = ?",
                (greatest, sid),
            )
            return len(elig_rows)

    async def snapshot_data(self, session_id: str) -> SessionSnapshotData:
        sid = _require_session_id(session_id)
        async with self._database.write_transaction() as conn:
            sess_cursor = await conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
                (sid,),
            )
            sess_row = await sess_cursor.fetchone()
            if sess_row is None:
                raise _session_not_found()
            session = _row_to_session(sess_row)
            snapshot_seq = session.last_seq

            runs_cursor = await conn.execute(
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE session_id = ? "
                "ORDER BY created_at DESC, run_id DESC LIMIT 101",
                (sid,),
            )
            run_rows = await runs_cursor.fetchall()
            has_earlier = len(run_rows) > 100
            newest = list(run_rows[:100])
            newest.reverse()
            runs = tuple(_row_to_run(r) for r in newest)
            earliest_included = runs[0].run_id if runs else None

            state_cursor = await conn.execute(
                "SELECT active_run_id FROM runtime_state WHERE singleton_id = 1"
            )
            state_row = await state_cursor.fetchone()
            active_id = state_row["active_run_id"] if state_row is not None else None
            active_run = None
            if active_id is not None:
                # Scope by the target session: a globally active run that
                # belongs to a different session must not appear in this
                # snapshot. A same-session active run is still returned even
                # when it falls outside the newest 100.
                active_cursor = await conn.execute(
                    f"SELECT {_RUN_COLUMNS} FROM runs "
                    "WHERE run_id = ? AND session_id = ?",
                    (active_id, sid),
                )
                active_row = await active_cursor.fetchone()
                if active_row is not None:
                    active_run = _row_to_run(active_row)
        return SessionSnapshotData(
            session=session,
            runs=runs,
            active_run=active_run,
            snapshot_seq=snapshot_seq,
            has_earlier_runs=has_earlier,
            earliest_included_run_id=earliest_included,
        )
