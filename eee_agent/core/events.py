from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping, TypeAlias, cast

from eee_agent.core.ids import IdKind, new_id, require_id

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
_FrozenJsonValue: TypeAlias = (
    JsonPrimitive
    | tuple["_FrozenJsonValue", ...]
    | Mapping[str, "_FrozenJsonValue"]
)
_EVENT_TYPE_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")
_EVENT_FIELDS = frozenset(
    {"event_id", "event_type", "timestamp", "payload", "schema_version"}
)


def _freeze_payload(payload: object) -> Mapping[str, _FrozenJsonValue]:
    if type(payload) is not dict:
        raise TypeError("payload must be an exact dict")
    return cast(Mapping[str, _FrozenJsonValue], _freeze_json(payload, set()))


def _freeze_json(value: object, active: set[int]) -> _FrozenJsonValue:
    # Direct exact-type branches (not a cached type() local) so mypy narrows the
    # value inside each branch: math.isfinite sees a float, dicts expose .items,
    # and lists are iterable. bool stays exact (it never matches the int branch
    # because bool is checked first via the exact `type(...) is bool` test done
    # implicitly by the `in (type(None), str, bool, int)` membership below — note
    # membership tests do not narrow, so each container branch re-checks).
    if type(value) is None.__class__ or type(value) is str or type(value) is bool:
        return cast(JsonPrimitive, value)
    if type(value) is int:
        return cast(JsonPrimitive, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("payload float values must be finite")
        return cast(float, value)
    if type(value) is dict:
        data = cast(dict[object, object], value)
        identity = id(value)
        if identity in active:
            raise ValueError("payload must not contain a cycle")
        active.add(identity)
        try:
            frozen: dict[str, _FrozenJsonValue] = {}
            for key, item in data.items():
                if type(key) is not str:
                    raise TypeError("payload keys must be exact strings")
                frozen[key] = _freeze_json(item, active)
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if type(value) is list:
        items = cast(list[object], value)
        identity = id(value)
        if identity in active:
            raise ValueError("payload must not contain a cycle")
        active.add(identity)
        try:
            return tuple(_freeze_json(item, active) for item in items)
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def _thaw_json(value: _FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in cast(tuple[_FrozenJsonValue, ...], value)]
    # Remaining _FrozenJsonValue members are exactly the JSON primitives
    # (str | int | float | bool | None), all valid JsonValue returns.
    return cast(JsonPrimitive, value)


def _require_str(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string")
    return value


def _require_exact_payload(value: object, label: str) -> Mapping[str, _FrozenJsonValue]:
    # DomainEvent.from_dict feeds the raw payload into __post_init__, which
    # re-validates and deep-freezes it. Accept any exact dict here so the
    # constructor boundary stays explicit; the freeze happens in post-init.
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return cast(Mapping[str, _FrozenJsonValue], value)


def _require_int(value: object, label: str) -> int:
    # Exact int only: bool is a subclass of int, so `type(value) is bool` must
    # be rejected explicitly to keep bool from passing an integer field.
    if type(value) is bool:
        raise TypeError(f"{label} must be an exact integer, not a bool")
    if type(value) is not int:
        raise TypeError(f"{label} must be an exact integer")
    return value


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    payload: Mapping[str, _FrozenJsonValue]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.event_id) is not str:
            raise TypeError("event_id must be an exact string")
        require_id(self.event_id, IdKind.EVENT)
        if type(self.event_type) is not str:
            raise TypeError("event_type must be an exact string")
        if _EVENT_TYPE_RE.fullmatch(self.event_type) is None:
            raise ValueError("event_type must be namespaced")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be the integer 1")
        if self.schema_version != 1:
            raise ValueError("only schema_version 1 is supported")
        if type(self.timestamp) is not datetime:
            raise TypeError("timestamp must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        object.__setattr__(
            self, "timestamp", self.timestamp.astimezone(timezone.utc)
        )
        object.__setattr__(self, "payload", _freeze_payload(self.payload))

    @classmethod
    def create(
        cls,
        *,
        event_type: str,
        payload: dict[str, JsonValue],
        timestamp: datetime | None = None,
    ) -> "DomainEvent":
        # The dataclass field is typed as the frozen payload, but __post_init__
        # re-validates and deep-freezes the mutable dict the caller passes.
        # Cast at this construction boundary (the value is exact-validated
        # immediately inside __post_init__ via _freeze_payload).
        return cls(
            event_id=new_id(IdKind.EVENT),
            event_type=event_type,
            timestamp=(
                datetime.now(timezone.utc) if timestamp is None else timestamp
            ),
            payload=cast(Mapping[str, _FrozenJsonValue], payload),
        )

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DomainEvent":
        if type(data) is not dict:
            raise TypeError("event data must be an exact dict")
        if (
            len(data) != len(_EVENT_FIELDS)
            or any(type(key) is not str for key in data)
            or set(data) != _EVENT_FIELDS
        ):
            raise ValueError("event data must contain exactly five fields")

        timestamp_text = _require_str(data["timestamp"], "timestamp")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp_text)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid ISO timestamp") from exc

        event_id = _require_str(data["event_id"], "event_id")
        event_type = _require_str(data["event_type"], "event_type")
        # schema_version must be an exact int; bool (an int subclass) is rejected
        # by _require_int so a JSON true/false can never satisfy this field.
        schema_version = _require_int(data["schema_version"], "schema_version")
        # payload is re-validated and deep-frozen inside __post_init__.
        payload = _require_exact_payload(data["payload"], "payload")

        return cls(
            event_id=event_id,
            event_type=event_type,
            timestamp=parsed_timestamp,
            payload=payload,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "payload": _thaw_json(self.payload),
            "schema_version": self.schema_version,
        }
