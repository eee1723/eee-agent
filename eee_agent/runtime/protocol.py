"""Strict Runtime wire protocol: command parsing and envelope serialization.

Parses inbound command messages into :class:`RuntimeCommand` and serializes
responses/events. All parse errors become structured ``AgentException`` with
protocol/validation codes; no raw traceback or parse detail is leaked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.core.events import JsonValue
from eee_agent.runtime.models import freeze_json, thaw_json

PROTOCOL = "eee.runtime/1"
MAX_MESSAGE_BYTES = 1_048_576
_MAX_REQUEST_ID_LEN = 128

COMMAND_TYPES = frozenset(
    {
        "runtime.ping",
        "session.list",
        "session.create",
        "session.rename",
        "session.archive",
        "session.delete",
        "session.subscribe",
        "session.snapshot",
        "events.replay",
        "run.start",
        "run.stop",
        "run.force_stop",
    }
)

DEFERRED_COMMAND_TYPES = frozenset(
    {
        "session.fork",
        "workspace.create",
        "workspace.bind",
        "workspace.switch",
        "workspace.inspect",
        "changeset.approve",
        "changeset.reject",
        "visual_policy.resolve",
        "artifact.reveal",
        "artifact.open",
        "artifact.copy_reference",
    }
)

_COMMAND_FIELDS = frozenset(
    {"protocol", "kind", "request_id", "type", "payload"}
)


# --- structured error factories -------------------------------------------


def _err(code: str, category: ErrorCategory, message: str) -> AgentException:
    return AgentException(
        AgentError(code=code, category=category, message_for_user=message)
    )


def _invalid_json() -> AgentException:
    return _err(
        "protocol.invalid_json",
        ErrorCategory.PROTOCOL,
        "The Runtime message is not valid JSON.",
    )


def _invalid_envelope() -> AgentException:
    return _err(
        "protocol.invalid_envelope",
        ErrorCategory.PROTOCOL,
        "The Runtime command envelope is invalid.",
    )


def _incompatible_version() -> AgentException:
    return _err(
        "protocol.incompatible_version",
        ErrorCategory.PROTOCOL,
        "The Runtime protocol version is incompatible.",
    )


def _unknown_command() -> AgentException:
    return _err(
        "protocol.unknown_command",
        ErrorCategory.PROTOCOL,
        "The Runtime command is unknown.",
    )


def _payload_too_large() -> AgentException:
    return _err(
        "validation.payload_too_large",
        ErrorCategory.VALIDATION,
        "The Runtime message exceeds the maximum allowed size.",
    )


class _DuplicateKeyError(ValueError):
    """Raised by the JSON object_pairs_hook on any duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """object_pairs_hook that rejects duplicate keys at any object depth."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def _snapshot_json(value: object) -> object:
    """Return a fresh, independent JSON tree copy.

    Rejects non-JSON values, cycles, and non-finite floats (including
    overflow-to-inf) at construction time so a later encode cannot fail.
    """
    return thaw_json(freeze_json(value))


@dataclass(frozen=True, slots=True)
class RuntimeCommand:
    """A parsed Runtime command envelope."""

    request_id: str
    command_type: str
    payload: dict[str, JsonValue]


def parse_command(raw: str | bytes | bytearray) -> RuntimeCommand:
    """Parse a Runtime command message.

    Accepts ``str`` or UTF-8 ``bytes``. Enforces the 1 MiB byte limit, the
    exact five input fields, exact JSON types, a non-empty request_id (<=128
    chars), a strict-JSON object payload, and a known command type. Known
    deferred command types are parsed (so the server can return
    ``runtime.capability_unavailable``); any other command type raises
    ``protocol.unknown_command``.
    """
    if type(raw) is str:
        data = raw.encode("utf-8")
    elif type(raw) is bytes:
        data = raw
    else:
        # Reject bytearray, memoryview, and str/bytes subclasses: the contract
        # accepts exactly str or bytes.
        raise _invalid_envelope()

    if len(data) > MAX_MESSAGE_BYTES:
        raise _payload_too_large()

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid_json() from exc

    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise _invalid_json() from exc
    except _DuplicateKeyError as exc:
        # Duplicate keys (at any depth) silently taking the last value would
        # violate the strict five-field / strict-JSON contract.
        raise _invalid_envelope() from exc

    if not isinstance(obj, dict):
        raise _invalid_envelope()
    if set(obj.keys()) != _COMMAND_FIELDS:
        raise _invalid_envelope()

    protocol = obj["protocol"]
    kind = obj["kind"]
    request_id = obj["request_id"]
    command_type = obj["type"]
    payload = obj["payload"]

    if not isinstance(protocol, str):
        raise _invalid_envelope()
    if protocol != PROTOCOL:
        if protocol.startswith("eee.runtime/"):
            raise _incompatible_version()
        raise _invalid_envelope()
    if not isinstance(kind, str) or kind != "command":
        raise _invalid_envelope()
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > _MAX_REQUEST_ID_LEN
    ):
        raise _invalid_envelope()
    if not isinstance(command_type, str) or not command_type:
        raise _invalid_envelope()
    if not isinstance(payload, dict):
        raise _invalid_envelope()

    # Strict-JSON validation: reject non-finite floats (incl. overflow inf),
    # non-string keys, cycles, and any non-JSON value. Returns a fresh plain
    # copy so callers cannot mutate the parsed structure.
    try:
        plain = thaw_json(freeze_json(payload))
    except (TypeError, ValueError) as exc:
        raise _invalid_envelope() from exc
    if not isinstance(plain, dict):
        raise _invalid_envelope()

    if (
        command_type not in COMMAND_TYPES
        and command_type not in DEFERRED_COMMAND_TYPES
    ):
        raise _unknown_command()

    return RuntimeCommand(
        request_id=request_id, command_type=command_type, payload=plain
    )


def success_response(request_id: str, result: object) -> dict[str, object]:
    """Build a fresh success response envelope.

    ``result`` is deep-snapshotted into an independent JSON tree (and rejected
    up front if it contains non-JSON values, cycles, or non-finite floats).
    """
    return {
        "protocol": PROTOCOL,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": _snapshot_json(result),
    }


def error_response(request_id: str, error: AgentError) -> dict[str, object]:
    """Build a fresh error response envelope.

    The ``error`` field is exactly ``error.to_dict()`` (a fresh dict).
    """
    return {
        "protocol": PROTOCOL,
        "kind": "response",
        "request_id": request_id,
        "ok": False,
        "error": error.to_dict(),
    }


def event_envelope(
    *,
    event_id: str,
    session_id: str,
    run_id: str | None,
    seq: int,
    timestamp: str,
    event_type: str,
    payload: dict[str, JsonValue],
    schema_version: int = 1,
) -> dict[str, object]:
    """Build a fresh event envelope.

    ``run_id`` is nullable for session events. ``payload`` is deep-snapshotted
    into an independent JSON tree (and rejected up front if it contains
    non-JSON values, cycles, or non-finite floats).
    """
    return {
        "protocol": PROTOCOL,
        "kind": "event",
        "event_id": event_id,
        "session_id": session_id,
        "run_id": run_id,
        "seq": seq,
        "timestamp": timestamp,
        "type": event_type,
        "payload": _snapshot_json(payload),
        "schema_version": schema_version,
    }


def encode_envelope(envelope: dict[str, object]) -> bytes:
    """Serialize an envelope to compact, sorted UTF-8 JSON bytes.

    Deterministic (sorted keys) and does not modify the input dict.
    """
    return (
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        .encode("utf-8")
    )
