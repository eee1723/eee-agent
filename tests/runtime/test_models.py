from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eee_agent.core import AgentError, ErrorCategory
from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionRecord,
    SessionStatus,
    require_transition,
)

_SESSION_ID = f"ses_{'0' * 32}"
_RUN_ID = f"run_{'0' * 32}"
_EVENT_ID = f"evt_{'0' * 32}"
NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _session(**overrides: object) -> SessionRecord:
    values: dict[str, object] = dict(
        session_id=_SESSION_ID,
        title="Table",
        status=SessionStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        last_seq=0,
        replay_floor_seq=0,
    )
    values.update(overrides)
    return SessionRecord(**values)  # type: ignore[arg-type]


def _run(**overrides: object) -> RunRecord:
    values: dict[str, object] = dict(
        run_id=_RUN_ID,
        session_id=_SESSION_ID,
        status=RunStatus.CREATED,
        user_input="inspect",
        final_response=None,
        created_at=NOW,
        started_at=None,
        finished_at=None,
        failure_json=None,
        model_snapshot_json={"model": "fake"},
    )
    values.update(overrides)
    return RunRecord(**values)  # type: ignore[arg-type]


def _event(**overrides: object) -> EventRecord:
    values: dict[str, object] = dict(
        event_id=_EVENT_ID,
        session_id=_SESSION_ID,
        run_id=None,
        seq=1,
        event_type="run.created",
        timestamp=NOW,
        payload={"answer": 42},
        retention_class=RetentionClass.DURABLE,
    )
    values.update(overrides)
    return EventRecord(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------

LEGAL_TRANSITIONS = [
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


@pytest.mark.parametrize(("current", "target"), LEGAL_TRANSITIONS)
def test_legal_transition_succeeds(current: RunStatus, target: RunStatus) -> None:
    require_transition(current, target)


ILLEGAL_TRANSITIONS = [
    (RunStatus.CREATED, RunStatus.PLANNING),
    (RunStatus.CREATED, RunStatus.FINALIZING),
    (RunStatus.CREATED, RunStatus.COMPLETED),
    (RunStatus.CREATED, RunStatus.STOPPING),
    (RunStatus.CREATED, RunStatus.CANCELLED),
    (RunStatus.CREATED, RunStatus.RETRYING),
    (RunStatus.PREPARING_CONTEXT, RunStatus.CREATED),
    (RunStatus.PREPARING_CONTEXT, RunStatus.FINALIZING),
    (RunStatus.PREPARING_CONTEXT, RunStatus.COMPLETED),
    (RunStatus.PREPARING_CONTEXT, RunStatus.CANCELLED),
    (RunStatus.PLANNING, RunStatus.CREATED),
    (RunStatus.PLANNING, RunStatus.PREPARING_CONTEXT),
    (RunStatus.PLANNING, RunStatus.CANCELLED),
    (RunStatus.RETRYING, RunStatus.CREATED),
    (RunStatus.RETRYING, RunStatus.COMPLETED),
    (RunStatus.RETRYING, RunStatus.CANCELLED),
    (RunStatus.RETRYING, RunStatus.FINALIZING),
    (RunStatus.FINALIZING, RunStatus.PLANNING),
    (RunStatus.FINALIZING, RunStatus.STOPPING),
    (RunStatus.FINALIZING, RunStatus.CANCELLED),
    (RunStatus.STOP_REQUESTED, RunStatus.PLANNING),
    (RunStatus.STOP_REQUESTED, RunStatus.CANCELLED),
    (RunStatus.STOP_REQUESTED, RunStatus.FAILED),
    (RunStatus.STOPPING, RunStatus.PLANNING),
    (RunStatus.STOPPING, RunStatus.STOP_REQUESTED),
]


@pytest.mark.parametrize(("current", "target"), ILLEGAL_TRANSITIONS)
def test_illegal_transition_raises(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(ValueError, match=f"{current.value} -> {target.value}"):
        require_transition(current, target)


TERMINAL_STATES = [RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED]


@pytest.mark.parametrize("terminal", TERMINAL_STATES)
@pytest.mark.parametrize("target", list(RunStatus))
def test_terminal_state_cannot_transition(
    terminal: RunStatus, target: RunStatus
) -> None:
    with pytest.raises(ValueError, match=f"{terminal.value} -> {target.value}"):
        require_transition(terminal, target)


def test_illegal_transition_message_contains_arrow() -> None:
    with pytest.raises(ValueError) as exc_info:
        require_transition(RunStatus.COMPLETED, RunStatus.PLANNING)
    assert "Completed -> Planning" in str(exc_info.value)


def test_retry_can_return_to_planning() -> None:
    require_transition(RunStatus.RETRYING, RunStatus.PLANNING)


# --------------------------------------------------------------------------
# SessionRecord
# --------------------------------------------------------------------------

def test_session_record_serializes_status_value() -> None:
    assert _session().to_dict()["status"] == "active"


def test_session_record_rejects_wrong_session_id_kind() -> None:
    with pytest.raises(ValueError, match="expected ses_ id"):
        _session(session_id=_RUN_ID)


def test_session_record_rejects_string_status() -> None:
    with pytest.raises(ValueError, match="status must be an exact SessionStatus"):
        _session(status="active")


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_session_record_rejects_naive_datetime(field: str) -> None:
    naive = datetime(2026, 7, 14)
    with pytest.raises(ValueError, match="timezone-aware"):
        _session(**{field: naive})


def test_session_record_normalizes_non_utc_aware_datetime() -> None:
    aware = datetime(2026, 7, 14, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    session = _session(created_at=aware, updated_at=aware)
    assert session.created_at == datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
    assert session.created_at.tzinfo is timezone.utc
    assert session.to_dict()["created_at"] == "2026-07-14T04:00:00+00:00"


def test_session_record_rejects_negative_last_seq() -> None:
    with pytest.raises(ValueError, match="last_seq"):
        _session(last_seq=-1)


def test_session_record_rejects_negative_replay_floor() -> None:
    with pytest.raises(ValueError, match="replay_floor_seq"):
        _session(replay_floor_seq=-1)


def test_session_record_rejects_floor_above_last_seq() -> None:
    with pytest.raises(ValueError, match="replay_floor_seq"):
        _session(last_seq=2, replay_floor_seq=3)


def test_session_record_rejects_bool_as_last_seq() -> None:
    with pytest.raises(TypeError, match="last_seq must be an integer"):
        _session(last_seq=True)


def test_session_record_rejects_bool_as_replay_floor() -> None:
    with pytest.raises(TypeError, match="replay_floor_seq must be an integer"):
        _session(replay_floor_seq=True)


def test_session_record_rejects_empty_title() -> None:
    with pytest.raises(ValueError, match="title"):
        _session(title="")


def test_session_record_rejects_non_string_title() -> None:
    with pytest.raises((TypeError, ValueError)):
        _session(title=123)


# --------------------------------------------------------------------------
# RunRecord
# --------------------------------------------------------------------------

def test_run_record_serializes_status_value() -> None:
    assert _run().to_dict()["status"] == "Created"


def test_run_record_rejects_wrong_run_id_kind() -> None:
    with pytest.raises(ValueError, match="expected run_ id"):
        _run(run_id=_SESSION_ID)


def test_run_record_rejects_wrong_session_id_kind() -> None:
    with pytest.raises(ValueError, match="expected ses_ id"):
        _run(session_id=_RUN_ID)


def test_run_record_rejects_string_status() -> None:
    with pytest.raises(ValueError, match="status must be an exact RunStatus"):
        _run(status="Created")


def test_run_record_rejects_non_string_user_input() -> None:
    with pytest.raises(TypeError, match="user_input"):
        _run(user_input=123)


def test_run_record_rejects_non_string_final_response() -> None:
    with pytest.raises(TypeError, match="final_response"):
        _run(final_response=123)


def test_run_record_rejects_naive_created_at() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _run(created_at=datetime(2026, 7, 14))


def test_run_record_normalizes_non_utc_aware_datetime() -> None:
    aware = datetime(2026, 7, 14, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    run = _run(created_at=aware, started_at=aware)
    assert run.created_at == datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
    assert run.started_at == datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)


def test_run_record_requires_model_snapshot_object() -> None:
    with pytest.raises(TypeError, match="model_snapshot_json"):
        _run(model_snapshot_json=[])


def test_run_record_failure_json_must_be_object_or_none() -> None:
    with pytest.raises(TypeError, match="failure_json"):
        _run(failure_json=[])


def test_run_record_model_snapshot_is_snapshot_of_input() -> None:
    source = {"model": "fake", "tokens": [1, 2]}
    run = _run(model_snapshot_json=source)
    source["model"] = "mutated"
    source["tokens"].append(3)
    assert run.to_dict()["model_snapshot_json"] == {"model": "fake", "tokens": [1, 2]}


def test_run_record_failure_json_is_snapshot_of_input() -> None:
    failure = AgentError(
        "runtime.failed", ErrorCategory.INTERNAL_INVARIANT, "boom"
    ).to_dict()
    run = _run(failure_json=failure)
    failure["code"] = "runtime.other"
    assert run.to_dict()["failure_json"]["code"] == "runtime.failed"


def test_run_record_model_snapshot_is_deeply_immutable() -> None:
    run = _run(model_snapshot_json={"model": {"name": "fake", "tokens": [1]}})
    with pytest.raises(TypeError):
        run.model_snapshot_json["new"] = True
    with pytest.raises(TypeError):
        run.model_snapshot_json["model"]["name"] = "x"


def test_run_record_failure_json_is_deeply_immutable() -> None:
    run = _run(
        failure_json=AgentError(
            "runtime.failed", ErrorCategory.INTERNAL_INVARIANT, "boom"
        ).to_dict()
    )
    assert run.failure_json is not None
    with pytest.raises(TypeError):
        run.failure_json["code"] = "x"


def test_run_record_to_dict_returns_independent_objects() -> None:
    run = _run(model_snapshot_json={"model": {"name": "fake"}})
    first = run.to_dict()
    first["model_snapshot_json"]["model"]["name"] = "mutated"
    second = run.to_dict()
    assert second["model_snapshot_json"] == {"model": {"name": "fake"}}
    assert first["model_snapshot_json"] is not second["model_snapshot_json"]


# --------------------------------------------------------------------------
# EventRecord
# --------------------------------------------------------------------------

def test_event_record_serializes_stable_values() -> None:
    data = _event().to_dict()
    assert data["retention_class"] == "durable"
    assert data["schema_version"] == 1


def test_event_record_rejects_wrong_event_id_kind() -> None:
    with pytest.raises(ValueError, match="expected evt_ id"):
        _event(event_id=_SESSION_ID)


def test_event_record_rejects_wrong_session_id_kind() -> None:
    with pytest.raises(ValueError, match="expected ses_ id"):
        _event(session_id=_RUN_ID)


def test_event_record_rejects_wrong_run_id_kind() -> None:
    with pytest.raises(ValueError, match="expected run_ id"):
        _event(run_id=_EVENT_ID)


def test_event_record_rejects_zero_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        _event(seq=0)


def test_event_record_rejects_negative_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        _event(seq=-3)


def test_event_record_rejects_bool_as_seq() -> None:
    with pytest.raises(TypeError, match="seq must be an integer"):
        _event(seq=True)


def test_event_record_rejects_string_retention() -> None:
    with pytest.raises(ValueError, match="retention_class must be an exact RetentionClass"):
        _event(retention_class="durable")


def test_event_record_requires_namespaced_event_type() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        _event(event_type="created")


def test_event_record_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _event(timestamp=datetime(2026, 7, 14))


def test_event_record_normalizes_non_utc_aware_timestamp() -> None:
    aware = datetime(2026, 7, 14, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    event = _event(timestamp=aware)
    assert event.timestamp == datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)
    assert event.to_dict()["timestamp"] == "2026-07-14T04:00:00+00:00"


@pytest.mark.parametrize(
    "schema_version", [True, 1.0, "1"], ids=["bool", "float", "str"]
)
def test_event_record_rejects_non_integer_schema_version(
    schema_version: object,
) -> None:
    with pytest.raises(TypeError, match="schema_version must be the integer 1"):
        _event(schema_version=schema_version)


@pytest.mark.parametrize("schema_version", [0, 2, -1])
def test_event_record_rejects_unsupported_schema_version(schema_version: int) -> None:
    with pytest.raises(ValueError, match="schema_version"):
        _event(schema_version=schema_version)


def test_event_record_requires_payload_object() -> None:
    with pytest.raises(TypeError, match="payload"):
        _event(payload=[])


def test_event_record_payload_is_snapshot_of_input() -> None:
    source = {"a": {"b": [1]}}
    event = _event(payload=source)
    source["a"]["b"].append(2)
    assert event.to_dict()["payload"] == {"a": {"b": [1]}}


def test_event_record_payload_is_deeply_immutable() -> None:
    event = _event(payload={"a": {"b": [1]}})
    with pytest.raises(TypeError):
        event.payload["new"] = True
    with pytest.raises(TypeError):
        event.payload["a"]["b"] = []


def test_event_record_to_dict_returns_independent_objects() -> None:
    event = _event(payload={"a": {"b": [1]}})
    first = event.to_dict()
    first["payload"]["a"]["b"].append(2)
    second = event.to_dict()
    assert second["payload"] == {"a": {"b": [1]}}
    assert first["payload"] is not second["payload"]


# --------------------------------------------------------------------------
# JSON strictness (Foundation parity) on the payload field
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_event_record_payload_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _event(payload={"value": value})


def test_event_record_payload_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError, match="keys"):
        _event(payload={1: "value"})


@pytest.mark.parametrize(
    "value",
    [{1, 2}, datetime(2026, 7, 14, tzinfo=timezone.utc), (1, 2)],
    ids=["set", "datetime", "tuple"],
)
def test_event_record_payload_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported JSON"):
        _event(payload={"value": value})


@pytest.mark.parametrize("container", [dict, list], ids=["dict", "list"])
def test_event_record_payload_rejects_cycles(container: type) -> None:
    cyclic: object
    if container is dict:
        cyclic = {}
        cyclic["self"] = cyclic  # type: ignore[index]
    else:
        cyclic = []
        cyclic.append(cyclic)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="cycle"):
        _event(payload={"value": cyclic})
