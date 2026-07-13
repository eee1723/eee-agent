import json
from datetime import datetime, timedelta, timezone, tzinfo
from types import MappingProxyType

import pytest

from eee_agent.core.events import DomainEvent


_VALID_EVENT_ID = f"evt_{'0' * 32}"


def _domain_event(**overrides: object) -> DomainEvent:
    values: dict[str, object] = {
        "event_id": _VALID_EVENT_ID,
        "event_type": "run.created",
        "timestamp": datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
        "payload": {},
        "schema_version": 1,
    }
    values.update(overrides)
    return DomainEvent(**values)


def _event_data(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "event_id": _VALID_EVENT_ID,
        "event_type": "run.created",
        "timestamp": "2026-07-13T04:00:00+00:00",
        "payload": {"models": ["deepseek-v4-pro"], "tokens": 12},
        "schema_version": 1,
    }
    values.update(overrides)
    return values


def test_domain_event_serializes_utc_timestamp_and_payload() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": "deepseek-v4-pro", "tokens": 12},
        timestamp=datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc),
    )

    data = event.to_dict()

    assert data["event_type"] == "provider.model_completed"
    assert data["timestamp"] == "2026-07-13T04:00:00+00:00"
    assert data["payload"] == {"model": "deepseek-v4-pro", "tokens": 12}
    assert data["schema_version"] == 1


def test_domain_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DomainEvent.create(
            event_type="run.created",
            payload={},
            timestamp=datetime(2026, 7, 13, 4, 0),
        )


def test_domain_event_requires_namespaced_type() -> None:
    with pytest.raises(ValueError, match="namespaced"):
        DomainEvent.create(event_type="created", payload={})


def test_domain_event_snapshots_nested_payload() -> None:
    source = {"model": {"name": "deepseek-v4-pro", "tokens": [12, 24]}}

    event = DomainEvent.create(event_type="provider.model_completed", payload=source)
    source["model"]["name"] = "mutated"
    source["model"]["tokens"].append(48)

    assert event.to_dict()["payload"] == {
        "model": {"name": "deepseek-v4-pro", "tokens": [12, 24]}
    }


def test_domain_event_payload_is_deeply_immutable() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": {"name": "deepseek-v4-pro", "tokens": [12]}},
    )

    with pytest.raises(TypeError):
        event.payload["new"] = True
    with pytest.raises(TypeError):
        event.payload["model"]["name"] = "mutated"
    with pytest.raises((AttributeError, TypeError)):
        event.payload["model"]["tokens"].append(24)


def test_domain_event_to_dict_returns_fresh_mutable_payloads() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": {"name": "deepseek-v4-pro", "tokens": [12]}},
    )

    first = event.to_dict()
    first["payload"]["model"]["name"] = "mutated"
    first["payload"]["model"]["tokens"].append(24)

    second = event.to_dict()
    assert second["payload"] == {
        "model": {"name": "deepseek-v4-pro", "tokens": [12]}
    }
    assert first["payload"] is not second["payload"]


def test_domain_event_json_safety_rejects_non_string_keys() -> None:
    with pytest.raises(TypeError, match="payload keys must be exact strings"):
        DomainEvent.create(event_type="run.created", payload={1: "value"})


@pytest.mark.parametrize(
    "value",
    [
        {1, 2},
        datetime(2026, 7, 13, tzinfo=timezone.utc),
        (1, 2),
        type("DictSubclass", (dict,), {})({"key": "value"}),
        type("ListSubclass", (list,), {})([1, 2]),
    ],
    ids=["set", "datetime", "tuple", "dict-subclass", "list-subclass"],
)
def test_domain_event_json_safety_rejects_unsupported_values(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported JSON value"):
        DomainEvent.create(event_type="run.created", payload={"value": value})


@pytest.mark.parametrize(
    "payload",
    [[], MappingProxyType({}), type("DictSubclass", (dict,), {})()],
    ids=["list", "mapping-proxy", "dict-subclass"],
)
def test_domain_event_json_safety_requires_exact_top_level_dict(
    payload: object,
) -> None:
    with pytest.raises(TypeError, match="payload must be an exact dict"):
        DomainEvent.create(event_type="run.created", payload=payload)


@pytest.mark.parametrize(
    "value",
    [
        type("StrSubclass", (str,), {})("value"),
        type("IntSubclass", (int,), {})(1),
        type("FloatSubclass", (float,), {})(1.0),
    ],
    ids=["str-subclass", "int-subclass", "float-subclass"],
)
def test_domain_event_json_safety_rejects_primitive_subclasses(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported JSON value"):
        DomainEvent.create(event_type="run.created", payload={"value": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_domain_event_json_safety_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        DomainEvent.create(event_type="run.created", payload={"value": value})


@pytest.mark.parametrize("container_type", [dict, list], ids=["dict", "list"])
def test_domain_event_json_safety_rejects_cycles(container_type: type) -> None:
    if container_type is dict:
        cyclic: object = {}
        cyclic["self"] = cyclic
    else:
        cyclic = []
        cyclic.append(cyclic)

    with pytest.raises(ValueError, match="cycle"):
        DomainEvent.create(event_type="run.created", payload={"value": cyclic})


def test_domain_event_json_safety_allows_shared_noncyclic_values() -> None:
    shared = [1, {"value": True}]

    event = DomainEvent.create(
        event_type="run.created", payload={"first": shared, "second": shared}
    )

    assert event.to_dict()["payload"] == {"first": shared, "second": shared}


def test_domain_event_json_safety_round_trips_through_strict_json() -> None:
    event = DomainEvent.create(
        event_type="provider.model_completed",
        payload={
            "model": "deepseek-v4-pro",
            "tokens": 12,
            "cached": False,
            "score": 0.75,
            "detail": None,
            "tags": ["provider", "model"],
        },
    )

    encoded = json.dumps(event.to_dict(), allow_nan=False)

    assert json.loads(encoded) == event.to_dict()


def test_domain_event_timestamp_normalizes_equal_instant_to_utc() -> None:
    event = DomainEvent.create(
        event_type="run.created",
        payload={},
        timestamp=datetime(
            2026, 7, 13, 12, 0, tzinfo=timezone(timedelta(hours=8))
        ),
    )

    assert event.timestamp == datetime(2026, 7, 13, 4, 0, tzinfo=timezone.utc)
    assert event.timestamp.tzinfo is timezone.utc
    assert event.to_dict()["timestamp"] == "2026-07-13T04:00:00+00:00"


@pytest.mark.parametrize("timestamp", [False, 0, "", object()])
def test_domain_event_timestamp_rejects_wrong_runtime_types(timestamp: object) -> None:
    with pytest.raises(TypeError, match="timestamp must be a datetime"):
        DomainEvent.create(
            event_type="run.created", payload={}, timestamp=timestamp
        )


def test_domain_event_timestamp_snapshots_mutable_timezone() -> None:
    class MutableTimezone(tzinfo):
        def __init__(self) -> None:
            self.offset = timedelta(hours=8)

        def utcoffset(self, value: datetime | None) -> timedelta:
            return self.offset

        def dst(self, value: datetime | None) -> timedelta:
            return timedelta(0)

    mutable_timezone = MutableTimezone()
    event = DomainEvent.create(
        event_type="run.created",
        payload={},
        timestamp=datetime(2026, 7, 13, 12, 0, tzinfo=mutable_timezone),
    )

    mutable_timezone.offset = timedelta(hours=9)

    assert event.to_dict()["timestamp"] == "2026-07-13T04:00:00+00:00"


@pytest.mark.parametrize(
    "event_id",
    [None, 1, False, type("StrSubclass", (str,), {})(_VALID_EVENT_ID)],
    ids=["none", "int", "bool", "str-subclass"],
)
def test_domain_event_identity_requires_exact_string(event_id: object) -> None:
    with pytest.raises(TypeError, match="event_id must be an exact string"):
        _domain_event(event_id=event_id)


def test_domain_event_identity_requires_valid_event_id() -> None:
    with pytest.raises(ValueError, match="expected evt_ id"):
        _domain_event(event_id=f"run_{'0' * 32}")


@pytest.mark.parametrize(
    "event_type",
    [None, 1, False, type("StrSubclass", (str,), {})("run.created")],
    ids=["none", "int", "bool", "str-subclass"],
)
def test_domain_event_type_requires_exact_string(event_type: object) -> None:
    with pytest.raises(TypeError, match="event_type must be an exact string"):
        _domain_event(event_type=event_type)


@pytest.mark.parametrize(
    "event_type",
    [
        "created",
        ".created",
        "run.",
        "run..created",
        "Run.created",
        "run.Created",
        "run-created.ok",
        "run.created-now",
        "run/created.ok",
        "run. created",
        "run.created\n",
    ],
)
def test_domain_event_type_requires_lowercase_namespaced_grammar(
    event_type: str,
) -> None:
    with pytest.raises(ValueError, match="namespaced"):
        _domain_event(event_type=event_type)


@pytest.mark.parametrize(
    "schema_version",
    [True, False, 1.0, "1", type("IntSubclass", (int,), {})(1)],
    ids=["true", "false", "float", "str", "int-subclass"],
)
def test_domain_event_version_requires_exact_integer(
    schema_version: object,
) -> None:
    with pytest.raises(TypeError, match="schema_version must be the integer 1"):
        _domain_event(schema_version=schema_version)


@pytest.mark.parametrize("schema_version", [0, -1, 2])
def test_domain_event_version_rejects_unsupported_integer(
    schema_version: int,
) -> None:
    with pytest.raises(ValueError, match="schema_version 1"):
        _domain_event(schema_version=schema_version)


def test_domain_event_from_dict_reconstructs_strict_json_round_trip() -> None:
    original = DomainEvent.create(
        event_type="provider.model_completed",
        payload={"model": "deepseek-v4-pro", "tokens": [12, 24]},
        timestamp=datetime(
            2026, 7, 13, 12, 0, tzinfo=timezone(timedelta(hours=8))
        ),
    )
    data = json.loads(json.dumps(original.to_dict(), allow_nan=False))

    replayed = DomainEvent.from_dict(data)

    assert replayed.to_dict() == original.to_dict()
    assert replayed.event_id == original.event_id


@pytest.mark.parametrize(
    "data",
    [[], MappingProxyType({}), type("DictSubclass", (dict,), {})(_event_data())],
    ids=["list", "mapping-proxy", "dict-subclass"],
)
def test_domain_event_from_dict_requires_exact_dict(data: object) -> None:
    with pytest.raises(TypeError, match="event data must be an exact dict"):
        DomainEvent.from_dict(data)


@pytest.mark.parametrize(
    "key",
    ["event_id", "event_type", "timestamp", "payload", "schema_version"],
)
def test_domain_event_from_dict_rejects_missing_fields(key: str) -> None:
    data = _event_data()
    del data[key]

    with pytest.raises(ValueError, match="exactly five fields"):
        DomainEvent.from_dict(data)


def test_domain_event_from_dict_rejects_extra_fields() -> None:
    with pytest.raises(ValueError, match="exactly five fields"):
        DomainEvent.from_dict(_event_data(extra=True))


def test_domain_event_from_dict_rejects_non_exact_string_envelope_keys() -> None:
    key = type("StrSubclass", (str,), {})("event_id")
    data = _event_data()
    data[key] = data.pop("event_id")

    with pytest.raises(ValueError, match="exactly five fields"):
        DomainEvent.from_dict(data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("event_id", 1, "event_id must be an exact string"),
        ("event_type", 1, "event_type must be an exact string"),
        ("timestamp", 1, "timestamp must be an exact string"),
        ("payload", [], "payload must be an exact dict"),
        ("schema_version", True, "schema_version must be the integer 1"),
    ],
)
def test_domain_event_from_dict_rejects_wrong_field_runtime_types(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        DomainEvent.from_dict(_event_data(**{field: value}))


@pytest.mark.parametrize(
    ("timestamp", "message"),
    [
        ("not-a-timestamp", "valid ISO timestamp"),
        ("2026-07-13T04:00:00", "timezone-aware"),
        ("2026-07-13", "timezone-aware"),
    ],
)
def test_domain_event_from_dict_rejects_invalid_timestamp(
    timestamp: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DomainEvent.from_dict(_event_data(timestamp=timestamp))


@pytest.mark.parametrize("schema_version", [0, 2])
def test_domain_event_from_dict_rejects_unsupported_schema(
    schema_version: int,
) -> None:
    with pytest.raises(ValueError, match="schema_version 1"):
        DomainEvent.from_dict(_event_data(schema_version=schema_version))


@pytest.mark.parametrize("payload", [{"bad": {1}}, ("not", "a", "dict")])
def test_domain_event_from_dict_rejects_invalid_payload(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        DomainEvent.from_dict(_event_data(payload=payload))
