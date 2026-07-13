from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeAlias

from eee_agent.core.ids import IdKind, new_id, require_id

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class DomainEvent:
    event_id: str
    event_type: str
    timestamp: datetime
    payload: dict[str, JsonValue]
    schema_version: int = 1

    def __post_init__(self) -> None:
        require_id(self.event_id, IdKind.EVENT)
        if "." not in self.event_type:
            raise ValueError("event_type must be namespaced")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")

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
            timestamp=timestamp or datetime.now(timezone.utc),
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload,
            "schema_version": self.schema_version,
        }
