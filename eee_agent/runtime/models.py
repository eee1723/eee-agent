from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from eee_agent.core import events as _events
from eee_agent.core.ids import IdKind, require_id
from eee_agent.core.strict_json import DuplicateKeyError, reject_duplicate_keys

# Namespaced event type grammar, matching Foundation DomainEvent exactly.
_EVENT_TYPE_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")


class SessionStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class RunStatus(StrEnum):
    CREATED = "Created"
    PREPARING_CONTEXT = "PreparingContext"
    PLANNING = "Planning"
    FINALIZING = "Finalizing"
    COMPLETED = "Completed"
    STOP_REQUESTED = "StopRequested"
    STOPPING = "Stopping"
    CANCELLED = "Cancelled"
    RETRYING = "Retrying"
    FAILED = "Failed"


# Single authority for the terminal Run statuses; repositories, the event
# store, and the service all derive from this set.
TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}
)


class RetentionClass(StrEnum):
    DURABLE = "durable"
    OPERATIONAL = "operational"


# Spec section 8.2: every legal Runtime run transition. Terminal states
# (Completed, Cancelled, Failed) have no outgoing edges.
_LEGAL_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({
        RunStatus.PREPARING_CONTEXT,
        RunStatus.STOP_REQUESTED,
        RunStatus.FAILED,
    }),
    RunStatus.PREPARING_CONTEXT: frozenset({
        RunStatus.PLANNING,
        RunStatus.STOP_REQUESTED,
        RunStatus.RETRYING,
        RunStatus.FAILED,
    }),
    RunStatus.PLANNING: frozenset({
        RunStatus.FINALIZING,
        RunStatus.STOP_REQUESTED,
        RunStatus.RETRYING,
        RunStatus.FAILED,
    }),
    RunStatus.RETRYING: frozenset({
        RunStatus.PREPARING_CONTEXT,
        RunStatus.PLANNING,
        RunStatus.STOP_REQUESTED,
        RunStatus.FAILED,
    }),
    RunStatus.FINALIZING: frozenset({
        RunStatus.COMPLETED,
        RunStatus.STOP_REQUESTED,
        RunStatus.FAILED,
    }),
    RunStatus.STOP_REQUESTED: frozenset({RunStatus.STOPPING}),
    RunStatus.STOPPING: frozenset({RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def require_transition(current: RunStatus, target: RunStatus) -> None:
    if type(current) is not RunStatus:
        raise ValueError("require_transition current must be an exact RunStatus")
    if type(target) is not RunStatus:
        raise ValueError("require_transition target must be an exact RunStatus")
    if target not in _LEGAL_TRANSITIONS[current]:
        raise ValueError(
            f"illegal Runtime transition: {current.value} -> {target.value}"
        )


def _require_utc_aware(field: str, value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


# --- Canonical JSON freeze/thaw (Foundation strictness) --------------------
# The single canonical implementation lives in ``eee_agent.core.events``;
# imported here so EventStore, the records, and existing consumers of this
# module share one helper.

freeze_json = _events.freeze_json
thaw_json = _events.thaw_json


def canonical_json_dumps(value: object) -> str:
    """Serialize a JSON value to canonical, sorted, compact, finite text.

    Reuses the strict ``freeze_json`` contract (rejects non-JSON values,
    non-string keys, cycles, and non-finite floats), then emits UTF-8-safe
    JSON with sorted keys, compact separators, and ``allow_nan=False``.
    Shared by RunRepository and EventStore so persisted JSON text is stable
    regardless of input key order.
    """
    return json.dumps(
        thaw_json(freeze_json(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_loads(text: object) -> object:
    """Parse canonical JSON text back into plain Python JSON values.

    Rejects duplicate object keys at any depth (mirroring the strict writer).
    The result is plain (not frozen); callers feed it back into a record
    constructor, which re-validates and deep-freezes it.
    """
    if type(text) is not str:
        raise TypeError("canonical JSON text must be an exact string")
    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid canonical JSON text: {exc}") from exc


# Tool-result preview cap for run-activity rendering. Shared by the agent
# runner (event payloads) and the runtime panel (activity view) so the two
# never drift.
TOOL_RESULT_PREVIEW_CHARS = 600


def canonical_digest(value: object) -> str:
    """SHA-256 hex digest of a value's canonical JSON (single authority)."""
    return hashlib.sha256(canonical_json_dumps(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_id: str
    title: str
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_seq: int
    replay_floor_seq: int

    def __post_init__(self) -> None:
        if type(self.session_id) is not str:
            raise TypeError("session_id must be an exact string")
        require_id(self.session_id, IdKind.SESSION)
        if type(self.title) is not str or not self.title:
            raise ValueError("title must be a non-empty string")
        if type(self.status) is not SessionStatus:
            raise ValueError("status must be an exact SessionStatus")
        object.__setattr__(
            self, "created_at", _require_utc_aware("created_at", self.created_at)
        )
        object.__setattr__(
            self, "updated_at", _require_utc_aware("updated_at", self.updated_at)
        )
        if type(self.last_seq) is not int:
            raise TypeError("last_seq must be an integer")
        if self.last_seq < 0:
            raise ValueError("last_seq must be >= 0")
        if type(self.replay_floor_seq) is not int:
            raise TypeError("replay_floor_seq must be an integer")
        if self.replay_floor_seq < 0:
            raise ValueError("replay_floor_seq must be >= 0")
        if self.replay_floor_seq > self.last_seq:
            raise ValueError("replay_floor_seq must be <= last_seq")

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "last_seq": self.last_seq,
            "replay_floor_seq": self.replay_floor_seq,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    session_id: str
    status: RunStatus
    user_input: str
    final_response: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    failure_json: Mapping[str, object] | None
    model_snapshot_json: Mapping[str, object]
    # D-2: snapshot of the deepagents TodoListMiddleware state captured at
    # run termination. Empty tuple means the run never wrote a todo list.
    # Kept optional + default-empty so legacy construction sites that do not
    # know about todos still work.
    todos: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        if type(self.run_id) is not str:
            raise TypeError("run_id must be an exact string")
        require_id(self.run_id, IdKind.RUN)
        if type(self.session_id) is not str:
            raise TypeError("session_id must be an exact string")
        require_id(self.session_id, IdKind.SESSION)
        if type(self.status) is not RunStatus:
            raise ValueError("status must be an exact RunStatus")
        if type(self.user_input) is not str:
            raise TypeError("user_input must be a string")
        if self.final_response is not None and type(self.final_response) is not str:
            raise TypeError("final_response must be a string or None")
        object.__setattr__(
            self, "created_at", _require_utc_aware("created_at", self.created_at)
        )
        if self.started_at is not None:
            object.__setattr__(
                self, "started_at", _require_utc_aware("started_at", self.started_at)
            )
        if self.finished_at is not None:
            object.__setattr__(
                self, "finished_at", _require_utc_aware("finished_at", self.finished_at)
            )
        if self.failure_json is not None:
            if type(self.failure_json) is not dict:
                raise TypeError("failure_json must be an exact dict or None")
            object.__setattr__(
                self, "failure_json", freeze_json(self.failure_json)
            )
        if type(self.model_snapshot_json) is not dict:
            raise TypeError("model_snapshot_json must be an exact dict")
        object.__setattr__(
            self, "model_snapshot_json", freeze_json(self.model_snapshot_json)
        )
        # D-2: todos is a tuple of mappings; freeze each entry defensively.
        if type(self.todos) is not tuple:
            raise TypeError("todos must be an exact tuple")
        frozen_todos = tuple(
            freeze_json(item) if type(item) is dict else item
            for item in self.todos
        )
        object.__setattr__(self, "todos", frozen_todos)

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status.value,
            "user_input": self.user_input,
            "final_response": self.final_response,
            "created_at": self.created_at.isoformat(),
            "started_at": (
                self.started_at.isoformat() if self.started_at is not None else None
            ),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at is not None else None
            ),
            "failure_json": (
                thaw_json(self.failure_json) if self.failure_json is not None else None
            ),
            "model_snapshot_json": thaw_json(self.model_snapshot_json),
            # D-2: thaw the frozen todo mappings back to plain dicts for the
            # JSON wire format.
            "todos": [thaw_json(item) if isinstance(item, Mapping) else dict(item)
                      for item in self.todos],
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    session_id: str
    run_id: str | None
    seq: int
    event_type: str
    timestamp: datetime
    payload: Mapping[str, object]
    retention_class: RetentionClass
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.event_id) is not str:
            raise TypeError("event_id must be an exact string")
        require_id(self.event_id, IdKind.EVENT)
        if type(self.session_id) is not str:
            raise TypeError("session_id must be an exact string")
        require_id(self.session_id, IdKind.SESSION)
        if self.run_id is not None:
            if type(self.run_id) is not str:
                raise TypeError("run_id must be a string or None")
            require_id(self.run_id, IdKind.RUN)
        if type(self.seq) is not int:
            raise TypeError("seq must be an integer")
        if self.seq <= 0:
            raise ValueError("seq must be > 0")
        if type(self.event_type) is not str:
            raise TypeError("event_type must be an exact string")
        if _EVENT_TYPE_RE.fullmatch(self.event_type) is None:
            raise ValueError("event_type must be namespaced")
        object.__setattr__(
            self, "timestamp", _require_utc_aware("timestamp", self.timestamp)
        )
        if type(self.payload) is not dict:
            raise TypeError("payload must be an exact dict")
        object.__setattr__(self, "payload", freeze_json(self.payload))
        if type(self.retention_class) is not RetentionClass:
            raise ValueError("retention_class must be an exact RetentionClass")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be the integer 1")
        if self.schema_version != 1:
            raise ValueError("only schema_version 1 is supported")

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "payload": thaw_json(self.payload),
            "retention_class": self.retention_class.value,
            "schema_version": self.schema_version,
        }
