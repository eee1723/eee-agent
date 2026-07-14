from __future__ import annotations

from datetime import datetime, timezone

from eee_agent.core import (
    AgentError,
    AgentException,
    ErrorCategory,
    IdKind,
    new_id,
    require_id,
)
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import SessionRecord, SessionStatus

_MAX_TITLE_CODEPOINTS = 200

_SESSION_COLUMNS = (
    "session_id, title, status, created_at, updated_at, last_seq, replay_floor_seq"
)
_SELECT_SESSION = f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?"
_SELECT_ACTIVE = (
    f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE status = 'active' "
    "ORDER BY created_at, session_id"
)
_SELECT_ALL = f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY created_at, session_id"
# The active-run guard JOINs runtime_state to runs and scopes by the target
# session, so a different session's active run never blocks this one.
_ACTIVE_RUN_FOR_SESSION = (
    "SELECT r.run_id FROM runtime_state rs "
    "JOIN runs r ON r.run_id = rs.active_run_id "
    "WHERE r.session_id = ?"
)


def _normalize_title(title: object) -> str:
    if type(title) is not str:
        raise TypeError("title must be a string")
    normalized = title.strip()
    if not normalized:
        raise ValueError("title must not be blank")
    if len(normalized) > _MAX_TITLE_CODEPOINTS:
        raise AgentException(
            AgentError(
                code="validation.payload_too_large",
                category=ErrorCategory.VALIDATION,
                message_for_user=(
                    f"Session title must be at most {_MAX_TITLE_CODEPOINTS} "
                    "characters."
                ),
            )
        )
    return normalized


def _require_session_id(session_id: object) -> str:
    if type(session_id) is not str:
        raise TypeError("session_id must be a string")
    return require_id(session_id, IdKind.SESSION)


def _session_not_found() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.session_not_found",
            category=ErrorCategory.VALIDATION,
            message_for_user="The Runtime session does not exist.",
        )
    )


def _active_run_error(run_id: str) -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.run_already_active",
            category=ErrorCategory.VALIDATION,
            message_for_user="The session has an active run and cannot be modified.",
            technical_detail_ref=run_id,
        )
    )


def _row_to_session(row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        title=row["title"],
        status=SessionStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_seq=row["last_seq"],
        replay_floor_seq=row["replay_floor_seq"],
    )


class SessionRepository:
    """Durable application-database persistence for Runtime sessions.

    Repository methods do not emit events and do not touch the events table or
    sequence bounds; event orchestration belongs to the service layer. Each
    mutating operation performs its checks and writes inside one
    ``RuntimeDatabase.write_transaction``.
    """

    def __init__(self, database: RuntimeDatabase) -> None:
        self._database = database

    async def create(self, title: str) -> SessionRecord:
        normalized = _normalize_title(title)
        session_id = new_id(IdKind.SESSION)
        now = datetime.now(timezone.utc)
        # The return record is built from the same values we are about to write,
        # so it is determined by this transaction and not by a post-commit re-read
        # that could observe a concurrent delete.
        record = SessionRecord(
            session_id=session_id,
            title=normalized,
            status=SessionStatus.ACTIVE,
            created_at=now,
            updated_at=now,
            last_seq=0,
            replay_floor_seq=0,
        )
        async with self._database.write_transaction() as conn:
            await conn.execute(
                "INSERT INTO sessions(session_id, title, status, created_at, "
                "updated_at, last_seq, replay_floor_seq) "
                "VALUES (?, ?, ?, ?, ?, 0, 0)",
                (
                    session_id,
                    normalized,
                    SessionStatus.ACTIVE.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return record

    async def get(self, session_id: str) -> SessionRecord:
        sid = _require_session_id(session_id)
        row = await self._database.fetchone(_SELECT_SESSION, (sid,))
        if row is None:
            raise _session_not_found()
        return _row_to_session(row)

    async def list(self, include_archived: bool = False) -> list[SessionRecord]:
        if type(include_archived) is not bool:
            raise TypeError("include_archived must be a bool")
        rows = await self._database.fetchall(
            _SELECT_ALL if include_archived else _SELECT_ACTIVE
        )
        return [_row_to_session(row) for row in rows]

    async def rename(self, session_id: str, title: str) -> SessionRecord:
        sid = _require_session_id(session_id)
        normalized = _normalize_title(title)
        now = datetime.now(timezone.utc)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(_SELECT_SESSION, (sid,))
            row = await cursor.fetchone()
            if row is None:
                raise _session_not_found()
            await conn.execute(
                "UPDATE sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (normalized, now.isoformat(), sid),
            )
            # Build the return record from the row read inside this transaction
            # plus the values just written; no post-commit re-read.
            record = SessionRecord(
                session_id=row["session_id"],
                title=normalized,
                status=SessionStatus(row["status"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=now,
                last_seq=row["last_seq"],
                replay_floor_seq=row["replay_floor_seq"],
            )
        return record

    async def archive(self, session_id: str) -> SessionRecord:
        sid = _require_session_id(session_id)
        now = datetime.now(timezone.utc)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(_SELECT_SESSION, (sid,))
            row = await cursor.fetchone()
            if row is None:
                raise _session_not_found()
            if row["status"] != SessionStatus.ARCHIVED.value:
                active_cursor = await conn.execute(_ACTIVE_RUN_FOR_SESSION, (sid,))
                active = await active_cursor.fetchone()
                if active is not None:
                    raise _active_run_error(active["run_id"])
                await conn.execute(
                    "UPDATE sessions SET status = ?, updated_at = ? "
                    "WHERE session_id = ?",
                    (SessionStatus.ARCHIVED.value, now.isoformat(), sid),
                )
                # Transition record built from this transaction's row plus the
                # new status/timestamp; no post-commit re-read.
                record = SessionRecord(
                    session_id=row["session_id"],
                    title=row["title"],
                    status=SessionStatus.ARCHIVED,
                    created_at=datetime.fromisoformat(row["created_at"]),
                    updated_at=now,
                    last_seq=row["last_seq"],
                    replay_floor_seq=row["replay_floor_seq"],
                )
            else:
                # Already archived: idempotent no-op, return the current row.
                record = _row_to_session(row)
        return record

    async def delete_application_records(self, session_id: str) -> None:
        sid = _require_session_id(session_id)
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
            )
            if await cursor.fetchone() is None:
                raise _session_not_found()
            active_cursor = await conn.execute(_ACTIVE_RUN_FOR_SESSION, (sid,))
            active = await active_cursor.fetchone()
            if active is not None:
                raise _active_run_error(active["run_id"])
            # FK ON DELETE CASCADE removes this session's runs and events.
            await conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        return None
