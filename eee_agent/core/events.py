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
    value_type = type(value)
    if value_type in (type(None), str, bool, int):
        return cast(JsonPrimitive, value)
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("payload float values must be finite")
        return cast(float, value)
    if value_type in (dict, list):
        identity = id(value)
        if identity in active:
            raise ValueError("payload must not contain a cycle")
        active.add(identity)
        try:
            if value_type is dict:
                frozen: dict[str, _FrozenJsonValue] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise TypeError("payload keys must be exact strings")
                    frozen[key] = _freeze_json(item, active)
                return MappingProxyType(frozen)
            return tuple(_freeze_json(item, active) for item in value)
        finally:
            active.remove(identity)
    raise TypeError(f"unsupported JSON value type: {value_type.__name__}")


def _thaw_json(value: _FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
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
        return cls(
            event_id=new_id(IdKind.EVENT),
            event_type=event_type,
            timestamp=(
                datetime.now(timezone.utc) if timestamp is None else timestamp
            ),
            payload=payload,
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

        timestamp = data["timestamp"]
        if type(timestamp) is not str:
            raise TypeError("timestamp must be an exact string")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ValueError("timestamp must be a valid ISO timestamp") from exc

        return cls(
            event_id=data["event_id"],
            event_type=data["event_type"],
            timestamp=parsed_timestamp,
            payload=data["payload"],
            schema_version=data["schema_version"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "payload": _thaw_json(self.payload),
            "schema_version": self.schema_version,
        }
