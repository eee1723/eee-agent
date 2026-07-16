"""Strict, UI-independent state helpers for the docked Runtime panel.

This module deliberately imports no Qt, SQLite, agent graph, Bridge transport,
or Houdini module. It validates the existing Runtime identity handoff, builds
the small read-only command subset used by Task 17-A, and tracks reconnect
cursors without owning persistence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

PROTOCOL = "eee.runtime/1"
MAX_MESSAGE_BYTES = 1_048_576
RUNTIME_DISCOVERY_FILENAME = "runtime.json"
RUNTIME_TOKEN_FILENAME = "runtime.token"
_DISCOVERY_FIELDS = frozenset(
    {
        "protocol",
        "host",
        "port",
        "pid",
        "process_nonce",
        "token_file",
        "token_fingerprint",
        "started_at",
    }
)
_READ_ONLY_COMMANDS = frozenset(
    {"runtime.ping", "session.list", "session.subscribe", "session.snapshot"}
)


class PanelClientError(ValueError):
    """Bounded panel-client failure that never includes a credential."""


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate key")
        result[key] = value
    return result


def _strict_json(raw: str | bytes, *, label: str) -> object:
    if type(raw) is str:
        data = raw.encode("utf-8")
    elif type(raw) is bytes:
        data = raw
    else:
        raise PanelClientError(f"{label} is invalid.")
    if len(data) > MAX_MESSAGE_BYTES:
        raise PanelClientError(f"{label} is too large.")
    try:
        text = data.decode("utf-8")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise PanelClientError(f"{label} is invalid.") from exc


def runtime_state_dir() -> Path:
    """Resolve the accepted Runtime state directory without creating it."""
    override = os.getenv("EEE_RUNTIME_HOME")
    if override is not None:
        if not override.strip():
            raise PanelClientError("Runtime home is invalid.")
        home = Path(override)
        if not home.is_absolute() or ".." in home.parts:
            raise PanelClientError("Runtime home is invalid.")
    else:
        local = os.getenv("LOCALAPPDATA")
        if not local:
            raise PanelClientError("Runtime home is unavailable.")
        home = Path(local) / "EEEAgent"
    return home.resolve() / "state"


@dataclass(frozen=True, slots=True)
class RuntimeCredentials:
    """Verified loopback Runtime endpoint and secret bearer credential."""

    host: str
    port: int
    process_nonce: str
    token_fingerprint: str
    token: str = field(repr=False)

    @property
    def websocket_url(self) -> str:
        return f"ws://{self.host}:{self.port}"


def load_runtime_credentials(state_dir: Path | str) -> RuntimeCredentials:
    """Read and verify `runtime.json` plus `runtime.token`.

    The full token is returned only in the repr-hidden ``token`` field. Every
    error is bounded and excludes file content.
    """
    state = Path(state_dir)
    try:
        raw_discovery = (state / RUNTIME_DISCOVERY_FILENAME).read_bytes()
    except OSError as exc:
        raise PanelClientError("Runtime is not running.") from exc
    discovery = _strict_json(raw_discovery, label="Runtime identity")
    if type(discovery) is not dict or set(discovery) != _DISCOVERY_FIELDS:
        raise PanelClientError("Runtime identity could not be verified.")
    if discovery["protocol"] != PROTOCOL:
        raise PanelClientError("Runtime protocol is incompatible.")
    if discovery["host"] != "127.0.0.1":
        raise PanelClientError("Runtime identity could not be verified.")
    port = discovery["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise PanelClientError("Runtime identity could not be verified.")
    if discovery["token_file"] != RUNTIME_TOKEN_FILENAME:
        raise PanelClientError("Runtime identity could not be verified.")
    nonce = discovery["process_nonce"]
    fingerprint = discovery["token_fingerprint"]
    if type(nonce) is not str or not nonce:
        raise PanelClientError("Runtime identity could not be verified.")
    if (
        type(fingerprint) is not str
        or len(fingerprint) != 12
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
    ):
        raise PanelClientError("Runtime identity could not be verified.")
    try:
        token = (state / RUNTIME_TOKEN_FILENAME).read_text(encoding="utf-8")
    except OSError as exc:
        raise PanelClientError("Runtime identity could not be verified.") from exc
    if not token or "\r" in token or "\n" in token:
        raise PanelClientError("Runtime identity could not be verified.")
    actual = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    if not hmac.compare_digest(actual, fingerprint):
        raise PanelClientError("Runtime identity could not be verified.")
    return RuntimeCredentials(
        host="127.0.0.1",
        port=port,
        process_nonce=nonce,
        token_fingerprint=fingerprint,
        token=token,
    )


def build_command(
    request_id: str, command_type: str, payload: Mapping[str, object]
) -> str:
    """Build one canonical Task 17-A read-only Runtime command."""
    if type(request_id) is not str or not request_id or len(request_id) > 128:
        raise PanelClientError("Runtime request id is invalid.")
    if command_type not in _READ_ONLY_COMMANDS:
        raise PanelClientError("Runtime command is not available to this panel.")
    if type(payload) is not dict:
        raise PanelClientError("Runtime command payload is invalid.")
    envelope = {
        "protocol": PROTOCOL,
        "kind": "command",
        "request_id": request_id,
        "type": command_type,
        "payload": payload,
    }
    try:
        return json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PanelClientError("Runtime command payload is invalid.") from exc


def parse_runtime_message(raw: str | bytes) -> Mapping[str, object]:
    """Parse a compatible Runtime response or event into an immutable mapping."""
    message = _strict_json(raw, label="Runtime message")
    if type(message) is not dict:
        raise PanelClientError("Runtime message is invalid.")
    if message.get("protocol") != PROTOCOL:
        raise PanelClientError("Runtime protocol is incompatible.")
    kind = message.get("kind")
    if kind not in ("response", "event"):
        raise PanelClientError("Runtime message is invalid.")
    if kind == "response":
        required = {"protocol", "kind", "request_id", "ok"}
        if not required.issubset(message):
            raise PanelClientError("Runtime response is invalid.")
        if type(message["request_id"]) is not str or type(message["ok"]) is not bool:
            raise PanelClientError("Runtime response is invalid.")
    else:
        required = {"protocol", "kind", "session_id", "seq", "type", "payload"}
        if not required.issubset(message):
            raise PanelClientError("Runtime event is invalid.")
        if type(message["session_id"]) is not str:
            raise PanelClientError("Runtime event is invalid.")
        seq = message["seq"]
        if seq is not None and (type(seq) is not int or seq <= 0):
            raise PanelClientError("Runtime event is invalid.")
        if type(message["type"]) is not str or type(message["payload"]) is not dict:
            raise PanelClientError("Runtime event is invalid.")
    return MappingProxyType(message)


def snapshot_boundary(
    message: Mapping[str, object],
) -> tuple[str, int] | None:
    """Return a validated Session snapshot recovery boundary, if present."""
    if message.get("kind") != "event" or message.get("type") != "session.snapshot":
        return None
    session_id = message.get("session_id")
    payload = message.get("payload")
    if type(session_id) is not str or not session_id or type(payload) is not dict:
        raise PanelClientError("Runtime snapshot is invalid.")
    snapshot_seq = payload.get("snapshot_seq")
    if type(snapshot_seq) is not int or snapshot_seq < 0:
        raise PanelClientError("Runtime snapshot is invalid.")
    return session_id, snapshot_seq


def choose_active_session(
    sessions: list[object], preferred_session_id: str | None = None
) -> Mapping[str, object] | None:
    """Choose the preferred active Session, otherwise the latest active one."""
    if type(sessions) is not list:
        raise PanelClientError("Runtime Session list is invalid.")
    active: list[dict[str, object]] = []
    for item in sessions:
        if type(item) is not dict:
            raise PanelClientError("Runtime Session list is invalid.")
        session_id = item.get("session_id")
        title = item.get("title")
        status = item.get("status")
        updated_at = item.get("updated_at")
        if (
            type(session_id) is not str
            or type(title) is not str
            or type(status) is not str
            or type(updated_at) is not str
        ):
            raise PanelClientError("Runtime Session list is invalid.")
        try:
            datetime.fromisoformat(updated_at)
        except ValueError as exc:
            raise PanelClientError("Runtime Session list is invalid.") from exc
        if status == "active":
            active.append(item)
    if preferred_session_id is not None:
        for item in active:
            if item["session_id"] == preferred_session_id:
                return MappingProxyType(dict(item))
    if not active:
        return None
    chosen = max(
        active,
        key=lambda item: (
            datetime.fromisoformat(item["updated_at"]),
            item["session_id"],
        ),
    )
    return MappingProxyType(dict(chosen))


class RuntimeCursorBook:
    """In-memory monotonic reconnect cursors, one per Runtime Session."""

    __slots__ = ("_last_seq",)

    def __init__(self) -> None:
        self._last_seq: dict[str, int] = {}

    def last_seq(self, session_id: str) -> int:
        if type(session_id) is not str or not session_id:
            raise PanelClientError("Runtime Session id is invalid.")
        return self._last_seq.get(session_id, 0)

    def advance(self, session_id: str, seq: int) -> bool:
        """Advance one Session to an explicit replay/snapshot boundary."""
        if type(session_id) is not str or not session_id:
            raise PanelClientError("Runtime Session id is invalid.")
        if type(seq) is not int or seq < 0:
            raise PanelClientError("Runtime sequence boundary is invalid.")
        previous = self._last_seq.get(session_id, 0)
        if seq <= previous:
            return False
        self._last_seq[session_id] = seq
        return True

    def observe(self, message: Mapping[str, object]) -> bool:
        """Advance from one parsed persisted event; return whether it advanced."""
        if message.get("kind") != "event":
            return False
        session_id = message.get("session_id")
        seq = message.get("seq")
        if type(session_id) is not str or not session_id:
            raise PanelClientError("Runtime event is invalid.")
        if seq is None:
            return False
        if type(seq) is not int or seq <= 0:
            raise PanelClientError("Runtime event is invalid.")
        return self.advance(session_id, seq)

    def snapshot(self) -> Mapping[str, int]:
        return MappingProxyType(dict(self._last_seq))
