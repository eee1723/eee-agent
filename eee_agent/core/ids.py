from __future__ import annotations

import re
from enum import StrEnum
from uuid import uuid4


class IdKind(StrEnum):
    SESSION = "ses"
    RUN = "run"
    EVENT = "evt"
    WORKSPACE = "ws"
    CHANGE = "chg"
    ARTIFACT = "art"


_PAYLOAD_RE = re.compile(r"^[0-9a-f]{32}$")


def new_id(kind: IdKind) -> str:
    return f"{kind.value}_{uuid4().hex}"


def require_id(value: str, kind: IdKind) -> str:
    prefix = f"{kind.value}_"
    if not value.startswith(prefix):
        raise ValueError(f"expected {prefix} id, got {value!r}")
    payload = value[len(prefix):]
    if not _PAYLOAD_RE.fullmatch(payload):
        raise ValueError(f"invalid {kind.name.lower()} id: {value!r}")
    return value
