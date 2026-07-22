from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from eee_agent.core import (
    AgentError,
    AgentException,
    ErrorCategory,
    IdKind,
    new_id,
    require_id,
)
from eee_agent.core.events import JsonValue
from eee_agent.runtime.database import RuntimeDatabase
from eee_agent.runtime.models import (
    RunRecord,
    RunStatus,
    SessionStatus,
    canonical_json_dumps,
    canonical_json_loads,
    require_transition,
)

_MAX_USER_INPUT_BYTES = 262_144
_MAX_FINAL_RESPONSE_BYTES = 1_048_576

# States that represent the model actively executing. started_at is stamped the
# first time a run enters one of these and never overwritten afterwards.
_ACTIVE_EXECUTION_STATES = frozenset({
    RunStatus.PREPARING_CONTEXT,
    RunStatus.PLANNING,
    RunStatus.FINALIZING,
    RunStatus.RETRYING,
})
_TERMINAL_STATES = frozenset({
    RunStatus.COMPLETED,
    RunStatus.CANCELLED,
    RunStatus.FAILED,
})
_TERMINAL_VALUES = tuple(state.value for state in _TERMINAL_STATES)

_RUN_COLUMNS = (
    "run_id, session_id, status, user_input, final_response, created_at, "
    "started_at, finished_at, failure_json, model_snapshot_json, todos_json"
)
_SELECT_RUN = f"SELECT {_RUN_COLUMNS} FROM runs WHERE run_id = ?"
_SELECT_RUNS_FOR_SESSION = (
    f"SELECT {_RUN_COLUMNS} FROM runs WHERE session_id = ? ORDER BY created_at, run_id"
)
_SELECT_NONTERMINAL = (
    f"SELECT {_RUN_COLUMNS} FROM runs WHERE status NOT IN "
    f"({','.join('?' for _ in _TERMINAL_VALUES)}) ORDER BY created_at, run_id"
)

_INTERRUPTED_ERROR = AgentError(
    code="runtime.interrupted",
    category=ErrorCategory.INTERNAL_INVARIANT,
    message_for_user=(
        "The previous Runtime process stopped before this run completed."
    ),
)


def _require_session_id(session_id: object) -> str:
    if type(session_id) is not str:
        raise TypeError("session_id must be a string")
    return require_id(session_id, IdKind.SESSION)


def _require_run_id(run_id: object) -> str:
    if type(run_id) is not str:
        raise TypeError("run_id must be a string")
    return require_id(run_id, IdKind.RUN)


def _validate_user_input(value: object) -> str:
    if type(value) is not str:
        raise TypeError("user_input must be a string")
    size = len(value.encode("utf-8"))
    if size < 1:
        raise ValueError("user_input must not be empty")
    if size > _MAX_USER_INPUT_BYTES:
        raise AgentException(
            AgentError(
                code="validation.payload_too_large",
                category=ErrorCategory.VALIDATION,
                message_for_user="Run user input exceeds the maximum allowed size.",
            )
        )
    return value


def _validate_final_response(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("final_response must be a string or None")
    if len(value.encode("utf-8")) > _MAX_FINAL_RESPONSE_BYTES:
        raise AgentException(
            AgentError(
                code="validation.payload_too_large",
                category=ErrorCategory.VALIDATION,
                message_for_user="Run final response exceeds the maximum allowed size.",
            )
        )
    return value


def _require_exact_mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    return value


def _session_not_found() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.session_not_found",
            category=ErrorCategory.VALIDATION,
            message_for_user="The Runtime session does not exist.",
        )
    )


def _session_archived() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.session_archived",
            category=ErrorCategory.VALIDATION,
            message_for_user="The Runtime session is archived.",
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


def _run_already_active(existing_run_id: str) -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.run_already_active",
            category=ErrorCategory.VALIDATION,
            message_for_user="A run is already active in this Runtime.",
            technical_detail_ref=existing_run_id,
        )
    )


def _invalid_transition(current: RunStatus, target: RunStatus) -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.invalid_run_transition",
            category=ErrorCategory.VALIDATION,
            message_for_user=(
                f"Run cannot transition {current.value} -> {target.value}."
            ),
        )
    )


def _row_to_run(row) -> RunRecord:
    # D-2: todos_json is nullable (legacy rows from schema v5 and runs that
    # never wrote a todo list). Thaw to a tuple of mappings; empty when NULL.
    todos_text = row["todos_json"] if "todos_json" in row.keys() else None
    todos: tuple[Mapping[str, object], ...] = ()
    if todos_text:
        thawed = canonical_json_loads(todos_text)
        if isinstance(thawed, list):
            todos = tuple(item for item in thawed if isinstance(item, Mapping))
    failure_value = (
        _require_exact_mapping(
            canonical_json_loads(row["failure_json"]), "stored run failure"
        )
        if row["failure_json"]
        else None
    )
    model_snapshot = _require_exact_mapping(
        canonical_json_loads(row["model_snapshot_json"]), "stored model snapshot"
    )
    return RunRecord(
        run_id=row["run_id"],
        session_id=row["session_id"],
        status=RunStatus(row["status"]),
        user_input=row["user_input"],
        final_response=row["final_response"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=(
            datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
        ),
        finished_at=(
            datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
        ),
        failure_json=failure_value,
        model_snapshot_json=model_snapshot,
        todos=todos,
    )


class RunRepository:
    """Durable application-database persistence for Runtime runs.

    Owns global one-active-run enforcement, legal state transitions, and
    interrupted-run reconciliation. Repository methods do not emit events and do
    not touch the events table or session sequence bounds; event orchestration
    belongs to the service layer.
    """

    def __init__(self, database: RuntimeDatabase) -> None:
        self._database = database

    async def create_and_acquire(
        self,
        session_id: str,
        user_input: str,
        model_snapshot: dict[str, JsonValue],
    ) -> RunRecord:
        sid = _require_session_id(session_id)
        validated_input = _validate_user_input(user_input)
        if type(model_snapshot) is not dict:
            raise TypeError("model_snapshot must be an exact dict")
        # Capture a single independent snapshot before the first await. Both the
        # persisted DB text and the returned record derive from it, so a caller
        # mutating the original dict while this coroutine awaits the write lock
        # cannot make them diverge. The original model_snapshot is never read
        # again after this point.
        snapshot_text = canonical_json_dumps(model_snapshot)
        snapshot_value = _require_exact_mapping(
            canonical_json_loads(snapshot_text), "model snapshot"
        )

        run_id = new_id(IdKind.RUN)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        record = RunRecord(
            run_id=run_id,
            session_id=sid,
            status=RunStatus.CREATED,
            user_input=validated_input,
            final_response=None,
            created_at=now,
            started_at=None,
            finished_at=None,
            failure_json=None,
            model_snapshot_json=snapshot_value,
        )
        async with self._database.write_transaction() as conn:
            sess_cursor = await conn.execute(
                "SELECT status FROM sessions WHERE session_id = ?", (sid,)
            )
            session_row = await sess_cursor.fetchone()
            if session_row is None:
                raise _session_not_found()
            if session_row["status"] != SessionStatus.ACTIVE.value:
                raise _session_archived()

            state_cursor = await conn.execute(
                "SELECT active_run_id FROM runtime_state WHERE singleton_id = 1"
            )
            state_row = await state_cursor.fetchone()
            existing = state_row["active_run_id"] if state_row is not None else None
            if existing is not None:
                raise _run_already_active(existing)

            await conn.execute(
                "INSERT INTO runs(run_id, session_id, status, user_input, "
                "final_response, created_at, started_at, finished_at, failure_json, "
                "model_snapshot_json) VALUES (?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, ?)",
                (
                    run_id,
                    sid,
                    RunStatus.CREATED.value,
                    validated_input,
                    now_iso,
                    snapshot_text,
                ),
            )
            await conn.execute(
                "UPDATE runtime_state SET active_run_id = ?, updated_at = ? "
                "WHERE singleton_id = 1",
                (run_id, now_iso),
            )
        return record

    async def get(self, run_id: str) -> RunRecord:
        rid = _require_run_id(run_id)
        row = await self._database.fetchone(_SELECT_RUN, (rid,))
        if row is None:
            raise _run_not_found()
        return _row_to_run(row)

    async def list_for_session(self, session_id: str) -> tuple[RunRecord, ...]:
        sid = _require_session_id(session_id)
        session_row = await self._database.fetchone(
            "SELECT 1 FROM sessions WHERE session_id = ?", (sid,)
        )
        if session_row is None:
            raise _session_not_found()
        rows = await self._database.fetchall(_SELECT_RUNS_FOR_SESSION, (sid,))
        return tuple(_row_to_run(row) for row in rows)

    async def active_run_id(self) -> str | None:
        row = await self._database.fetchone(
            "SELECT active_run_id FROM runtime_state WHERE singleton_id = 1"
        )
        if row is None:
            return None
        value = row["active_run_id"]
        if value is None:
            return None
        return require_id(value, IdKind.RUN)

    async def update_todos(
        self,
        run_id: str,
        todos: Sequence[Mapping[str, object]],
    ) -> None:
        """D-2: persist the latest deepagents TodoList state for a run.

        Idempotent and best-effort at the row level: callers invoke this on
        every todos.updated event so the latest plan survives a Runtime
        restart and can seed the next run. Does NOT change run status or
        timestamps; the run continues to flow through transition() normally.
        """
        rid = _require_run_id(run_id)
        if not isinstance(todos, (list, tuple)):
            raise TypeError("todos must be a list or tuple")
        # Canonical-JSON round-trip so the stored text matches the wire shape
        # and a caller mutating the input after this call cannot affect the
        # persisted copy.
        payload = [dict(item) for item in todos if isinstance(item, Mapping)]
        payload_text = canonical_json_dumps(payload) if payload else None
        async with self._database.write_transaction() as conn:
            await conn.execute(
                "UPDATE runs SET todos_json = ? WHERE run_id = ?",
                (payload_text, rid),
            )

    async def transition(
        self,
        run_id: str,
        target: RunStatus,
        *,
        final_response: str | None = None,
    ) -> RunRecord:
        if type(target) is not RunStatus:
            raise TypeError("target must be an exact RunStatus")
        rid = _require_run_id(run_id)
        new_final_response = _validate_final_response(final_response)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(_SELECT_RUN, (rid,))
            row = await cursor.fetchone()
            if row is None:
                raise _run_not_found()
            current = RunStatus(row["status"])
            try:
                require_transition(current, target)
            except ValueError as exc:
                raise _invalid_transition(current, target) from exc

            existing_started = row["started_at"]
            if target in _ACTIVE_EXECUTION_STATES and existing_started is None:
                started_iso: str | None = now_iso
            else:
                started_iso = existing_started
            if target in _TERMINAL_STATES:
                finished_iso: str | None = now_iso
            else:
                finished_iso = row["finished_at"]
            if new_final_response is None:
                effective_final = row["final_response"]
            else:
                effective_final = new_final_response

            await conn.execute(
                "UPDATE runs SET status = ?, started_at = ?, finished_at = ?, "
                "final_response = ? WHERE run_id = ?",
                (target.value, started_iso, finished_iso, effective_final, rid),
            )
            if target in _TERMINAL_STATES:
                await conn.execute(
                    "UPDATE runtime_state SET active_run_id = NULL, updated_at = ? "
                    "WHERE singleton_id = 1 AND active_run_id = ?",
                    (now_iso, rid),
                )
            after_cursor = await conn.execute(_SELECT_RUN, (rid,))
            after_row = await after_cursor.fetchone()
            record = _row_to_run(after_row)
        return record

    async def fail(self, run_id: str, error: AgentError) -> RunRecord:
        if type(error) is not AgentError:
            raise TypeError("error must be an AgentError")
        rid = _require_run_id(run_id)
        error_dict = error.to_dict()
        error_text = canonical_json_dumps(error_dict)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(_SELECT_RUN, (rid,))
            row = await cursor.fetchone()
            if row is None:
                raise _run_not_found()
            current = RunStatus(row["status"])
            try:
                require_transition(current, RunStatus.FAILED)
            except ValueError as exc:
                raise _invalid_transition(current, RunStatus.FAILED) from exc
            await conn.execute(
                "UPDATE runs SET status = ?, finished_at = ?, failure_json = ? "
                "WHERE run_id = ?",
                (RunStatus.FAILED.value, now_iso, error_text, rid),
            )
            await conn.execute(
                "UPDATE runtime_state SET active_run_id = NULL, updated_at = ? "
                "WHERE singleton_id = 1 AND active_run_id = ?",
                (now_iso, rid),
            )
            after_cursor = await conn.execute(_SELECT_RUN, (rid,))
            after_row = await after_cursor.fetchone()
            record = _row_to_run(after_row)
        return record

    async def reconcile_interrupted(self) -> tuple[RunRecord, ...]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        error_text = canonical_json_dumps(_INTERRUPTED_ERROR.to_dict())
        async with self._database.write_transaction() as conn:
            cursor = await conn.execute(_SELECT_NONTERMINAL, _TERMINAL_VALUES)
            rows = await cursor.fetchall()
            changed: list[RunRecord] = []
            for row in rows:
                await conn.execute(
                    "UPDATE runs SET status = ?, finished_at = ?, failure_json = ? "
                    "WHERE run_id = ?",
                    (RunStatus.FAILED.value, now_iso, error_text, row["run_id"]),
                )
                after_cursor = await conn.execute(_SELECT_RUN, (row["run_id"],))
                changed.append(_row_to_run(await after_cursor.fetchone()))
            await conn.execute(
                "UPDATE runtime_state SET active_run_id = NULL, updated_at = ? "
                "WHERE singleton_id = 1",
                (now_iso,),
            )
            return tuple(changed)
