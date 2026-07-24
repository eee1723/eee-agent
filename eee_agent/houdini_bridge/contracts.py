"""Strict read-only HoudiniBridge DTO and error contracts.

Frozen, JSON-canonical data transfer objects for the loopback-only read-only
HoudiniBridge. This module defines only the contract surface: request/response
envelopes, the single read-only ``scene.query`` operation, scene bindings,
bounded node facts, and structured bridge errors.

It deliberately imports **neither** ``hou`` **nor** ``rpyc``. Transport, token
authentication, the Houdini main-thread queue, and the Houdini-side adapter are
the responsibility of later tasks (15-B / 15-C / 15-D).

Strictness rules (mirroring ``eee_agent.runtime.models``):

* exact primitive types — ``bool`` is never accepted where an ``int`` is required;
* exact ``dict``/``list`` inputs; all mutable inputs are deep-frozen at
  construction so a caller cannot mutate a DTO after the fact;
* canonical JSON: sorted keys, compact separators, ``ensure_ascii=False``,
  ``allow_nan=False`` — rejects non-finite floats, non-string keys, cycles, and
  any non-JSON value;
* request parsing rejects duplicate object keys at any depth and payloads whose
  UTF-8 encoding exceeds 1 MiB.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from eee_agent.core.strict_json import (
    DuplicateKeyError,
    load_strict_json,
    reject_duplicate_keys,
)
from eee_agent.runtime.models import (
    canonical_json_dumps,
    freeze_json,
    thaw_json,
)

# --- protocol constants ----------------------------------------------------

PROTOCOL = "eee.bridge/1"
MAX_MESSAGE_BYTES = 1_048_576
MAX_DEADLINE_MS = 30_000
MIN_DEADLINE_MS = 1
_MAX_REQUEST_ID_LEN = 128
_MAX_DEADLINE_MS = MAX_DEADLINE_MS
_MAX_NODE_PATHS = 128

# The only fields a scene.query request payload may carry.
_SCENE_QUERY_PAYLOAD_FIELDS = frozenset(
    {"include_selection", "node_paths", "include_geometry_stats"}
)

_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "kind",
        "request_id",
        "operation",
        "deadline_ms",
        "scene_epoch",
        "payload",
    }
)
_BINDING_FIELDS = frozenset(
    {"instance_id", "scene_epoch", "hip_path", "observed_revision"}
)
_NODE_FIELDS = frozenset(
    {"path", "node_type", "parent_path", "display_name", "is_locked", "geometry_stats"}
)
_RESULT_FIELDS = frozenset({"binding", "selected_nodes", "nodes"})
_ERROR_FIELDS = frozenset(
    {"code", "category", "message_for_user", "retryable", "technical_detail_ref"}
)
_RESPONSE_REQUIRED_FIELDS = frozenset({"protocol", "kind", "request_id", "ok"})

# Bridge error codes are dot-separated lowercase, matching Foundation AgentError.
_CODE_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")


class BridgeOperation(StrEnum):
    """The single read-only operation exposed by the secure bridge."""

    SCENE_QUERY = "scene.query"


# --- strict JSON parsing helpers -------------------------------------------
# Shared implementation lives in ``eee_agent.core.strict_json``; the private
# names below are kept as thin aliases so existing importers are unchanged.

_DuplicateKeyError = DuplicateKeyError
_reject_duplicate_keys = reject_duplicate_keys


def _load_strict_json(raw: object, label: str) -> object:
    """Parse ``str``/``bytes`` into a strict JSON value.

    Rejects non-str/bytes input, payloads over :data:`MAX_MESSAGE_BYTES` UTF-8
    bytes, invalid UTF-8, invalid JSON, and duplicate object keys at any depth.
    """
    return load_strict_json(raw, label, max_bytes=MAX_MESSAGE_BYTES)


def _require_exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return value


def _require_exact_keys(value: dict[str, object], allowed: frozenset[str], label: str) -> None:
    if set(value.keys()) != allowed:
        raise ValueError(f"{label} must have exactly the required fields")


def _require_non_empty_str(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _require_exact_int(value: object, label: str) -> int:
    # Exact int only: bool is an int subclass, so reject it explicitly.
    if type(value) is bool:
        raise TypeError(f"{label} must be an integer, not a bool")
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


def _require_exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")
    return value


def _require_exact_str(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    return value


def _require_exact_mapping(value: object, label: str) -> Mapping[str, object]:
    # Accept any exact dict at the decode boundary; nested from_dict() callers
    # re-validate the field set and each field's type.
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return cast(Mapping[str, object], value)


def _require_exact_list(value: object, label: str) -> list[object]:
    # Returns the exact list so callers can iterate/construct a tuple; element
    # types are validated by the per-element decoder, not here.
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact list")
    return cast(list[object], value)


# --- scene.query payload ---------------------------------------------------


def _validate_scene_query_payload(payload: dict[str, object]) -> Mapping[str, object]:
    """Validate and deep-freeze a scene.query request payload."""
    unknown = set(payload.keys()) - _SCENE_QUERY_PAYLOAD_FIELDS
    if unknown:
        raise ValueError(
            f"scene.query payload has unknown fields: {sorted(map(str, unknown))}"
        )
    if "include_selection" in payload:
        _require_exact_bool(payload["include_selection"], "include_selection")
    if "include_geometry_stats" in payload:
        _require_exact_bool(
            payload["include_geometry_stats"], "include_geometry_stats"
        )
    if "node_paths" in payload:
        node_paths = payload["node_paths"]
        if type(node_paths) is not list:
            raise TypeError("node_paths must be a list")
        if len(node_paths) > _MAX_NODE_PATHS:
            raise ValueError(
                f"node_paths must contain at most {_MAX_NODE_PATHS} paths"
            )
        for path in node_paths:
            if type(path) is not str or not path or not path.startswith("/"):
                raise ValueError(
                    "node_paths must contain absolute Houdini node paths"
                )
    # Deep-freeze: rejects non-finite floats, non-string nested keys, cycles,
    # and any non-JSON value, and returns an immutable snapshot.
    return freeze_json(payload)  # type: ignore[return-value]


# --- DTOs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SceneBinding:
    """Immutable read result binding a query to a Houdini scene state."""

    instance_id: str
    scene_epoch: int
    hip_path: str | None
    observed_revision: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.instance_id, "SceneBinding.instance_id")
        _require_exact_int(self.scene_epoch, "SceneBinding.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("SceneBinding.scene_epoch must be >= 1")
        if self.hip_path is not None:
            _require_non_empty_str(self.hip_path, "SceneBinding.hip_path")
        _require_non_empty_str(self.observed_revision, "SceneBinding.observed_revision")

    def to_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "scene_epoch": self.scene_epoch,
            "hip_path": self.hip_path,
            "observed_revision": self.observed_revision,
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SceneBinding:
        envelope = _require_exact_dict(data, "SceneBinding envelope")
        _require_exact_keys(envelope, _BINDING_FIELDS, "SceneBinding")
        return cls(**envelope)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SelectedNode:
    """A bounded, read-only fact about a single selected or requested node."""

    path: str
    node_type: str
    parent_path: str
    display_name: str
    is_locked: bool
    geometry_stats: Mapping[str, object] | None

    def __post_init__(self) -> None:
        for name in ("path", "node_type", "parent_path", "display_name"):
            _require_non_empty_str(getattr(self, name), f"SelectedNode.{name}")
        _require_exact_bool(self.is_locked, "SelectedNode.is_locked")
        if self.geometry_stats is None:
            return
        if type(self.geometry_stats) is not dict:
            raise TypeError("SelectedNode.geometry_stats must be an exact dict or None")
        object.__setattr__(
            self, "geometry_stats", freeze_json(self.geometry_stats)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "node_type": self.node_type,
            "parent_path": self.parent_path,
            "display_name": self.display_name,
            "is_locked": self.is_locked,
            "geometry_stats": (
                thaw_json(self.geometry_stats)
                if self.geometry_stats is not None
                else None
            ),
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SelectedNode:
        envelope = _require_exact_dict(data, "SelectedNode envelope")
        _require_exact_keys(envelope, _NODE_FIELDS, "SelectedNode")
        return cls(**envelope)  # type: ignore[arg-type]


def _freeze_selected_nodes(
    value: object, field_name: str
) -> tuple[SelectedNode, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(
            f"SceneQueryResult.{field_name} must be a sequence of SelectedNode"
        )
    items = tuple(value)
    for item in items:
        if type(item) is not SelectedNode:
            raise TypeError(
                f"SceneQueryResult.{field_name} must contain only SelectedNode"
            )
    return items


@dataclass(frozen=True, slots=True)
class SceneQueryResult:
    """The bounded read-only result of a ``scene.query`` operation."""

    binding: SceneBinding
    selected_nodes: tuple[SelectedNode, ...]
    nodes: tuple[SelectedNode, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not SceneBinding:
            raise TypeError("SceneQueryResult.binding must be an exact SceneBinding")
        object.__setattr__(
            self,
            "selected_nodes",
            _freeze_selected_nodes(self.selected_nodes, "selected_nodes"),
        )
        object.__setattr__(
            self, "nodes", _freeze_selected_nodes(self.nodes, "nodes")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "selected_nodes": [node.to_dict() for node in self.selected_nodes],
            "nodes": [node.to_dict() for node in self.nodes],
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> SceneQueryResult:
        envelope = _require_exact_dict(data, "SceneQueryResult envelope")
        _require_exact_keys(envelope, _RESULT_FIELDS, "SceneQueryResult")
        binding = SceneBinding.from_dict(
            _require_exact_mapping(envelope["binding"], "SceneQueryResult.binding")
        )
        selected_nodes = tuple(
            SelectedNode.from_dict(
                _require_exact_mapping(node, "SceneQueryResult.selected_nodes[]")
            )
            for node in _require_exact_list(
                envelope["selected_nodes"], "SceneQueryResult.selected_nodes"
            )
        )
        nodes = tuple(
            SelectedNode.from_dict(
                _require_exact_mapping(node, "SceneQueryResult.nodes[]")
            )
            for node in _require_exact_list(
                envelope["nodes"], "SceneQueryResult.nodes"
            )
        )
        return cls(
            binding=binding,
            selected_nodes=selected_nodes,
            nodes=nodes,
        )


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    """A parsed, validated read-only bridge request envelope."""

    request_id: str
    operation: BridgeOperation
    deadline_ms: int
    scene_epoch: int
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.request_id) is not str:
            raise TypeError("BridgeRequest.request_id must be a string")
        if not self.request_id or len(self.request_id) > _MAX_REQUEST_ID_LEN:
            raise ValueError(
                "BridgeRequest.request_id must be a non-empty string (<=128 chars)"
            )
        if type(self.operation) is not BridgeOperation:
            raise TypeError(
                "BridgeRequest.operation must be an exact BridgeOperation"
            )
        _require_exact_int(self.deadline_ms, "BridgeRequest.deadline_ms")
        if self.deadline_ms < 1 or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError(
                "BridgeRequest.deadline_ms must be in 1..30000"
            )
        _require_exact_int(self.scene_epoch, "BridgeRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("BridgeRequest.scene_epoch must be >= 1")
        if type(self.payload) is not dict:
            raise TypeError("BridgeRequest.payload must be an exact dict")
        object.__setattr__(
            self, "payload", _validate_scene_query_payload(self.payload)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": self.operation.value,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": thaw_json(self.payload),
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BridgeRequest:
        envelope = _require_exact_dict(data, "BridgeRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "BridgeRequest envelope")
        protocol = envelope["protocol"]
        kind = envelope["kind"]
        operation = envelope["operation"]
        if type(protocol) is not str or protocol != PROTOCOL:
            raise ValueError("BridgeRequest protocol must be eee.bridge/1")
        if type(kind) is not str or kind != "request":
            raise ValueError("BridgeRequest kind must be request")
        if (
            type(operation) is not str
            or operation != BridgeOperation.SCENE_QUERY.value
        ):
            raise ValueError("BridgeRequest operation must be scene.query")
        request_id = _require_non_empty_str(
            envelope["request_id"], "BridgeRequest.request_id"
        )
        deadline_ms = _require_exact_int(
            envelope["deadline_ms"], "BridgeRequest.deadline_ms"
        )
        scene_epoch = _require_exact_int(
            envelope["scene_epoch"], "BridgeRequest.scene_epoch"
        )
        payload = _require_exact_mapping(
            envelope["payload"], "BridgeRequest.payload"
        )
        return cls(
            request_id=request_id,
            operation=BridgeOperation.SCENE_QUERY,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class BridgeError:
    """A structured, leak-free bridge error returned in a response."""

    code: str
    category: str
    message_for_user: str
    retryable: bool
    technical_detail_ref: str | None

    def __post_init__(self) -> None:
        if type(self.code) is not str or _CODE_RE.fullmatch(self.code) is None:
            raise ValueError("BridgeError.code must be a namespaced value")
        _require_non_empty_str(self.category, "BridgeError.category")
        _require_non_empty_str(self.message_for_user, "BridgeError.message_for_user")
        _require_exact_bool(self.retryable, "BridgeError.retryable")
        if self.technical_detail_ref is not None:
            _require_non_empty_str(
                self.technical_detail_ref, "BridgeError.technical_detail_ref"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "message_for_user": self.message_for_user,
            "retryable": self.retryable,
            "technical_detail_ref": self.technical_detail_ref,
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BridgeError:
        envelope = _require_exact_dict(data, "BridgeError envelope")
        _require_exact_keys(envelope, _ERROR_FIELDS, "BridgeError")
        return cls(**envelope)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class BridgeResponse:
    """A parsed, validated read-only bridge response envelope."""

    request_id: str
    result: SceneQueryResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        if type(self.request_id) is not str:
            raise TypeError("BridgeResponse.request_id must be a string")
        if not self.request_id or len(self.request_id) > _MAX_REQUEST_ID_LEN:
            raise ValueError(
                "BridgeResponse.request_id must be a non-empty string (<=128 chars)"
            )
        if self.result is not None and type(self.result) is not SceneQueryResult:
            raise TypeError(
                "BridgeResponse.result must be an exact SceneQueryResult or None"
            )
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError(
                "BridgeResponse.error must be an exact BridgeError or None"
            )
        # Exactly one of result/error must be present.
        if (self.result is None) == (self.error is None):
            raise ValueError(
                "BridgeResponse must carry exactly one of result or error"
            )

    def to_dict(self) -> dict[str, object]:
        if self.result is not None:
            return {
                "protocol": PROTOCOL,
                "kind": "response",
                "request_id": self.request_id,
                "ok": True,
                "result": self.result.to_dict(),
            }
        return {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": False,
            "error": self.error.to_dict(),  # type: ignore[union-attr]
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> BridgeResponse:
        envelope = _require_exact_dict(data, "BridgeResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("BridgeResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("BridgeResponse envelope has unknown fields")
        protocol = envelope["protocol"]
        kind = envelope["kind"]
        ok = envelope["ok"]
        if type(protocol) is not str or protocol != PROTOCOL:
            raise ValueError("BridgeResponse protocol must be eee.bridge/1")
        if type(kind) is not str or kind != "response":
            raise ValueError("BridgeResponse kind must be response")
        _require_exact_bool(ok, "BridgeResponse.ok")
        result = envelope.get("result")
        error = envelope.get("error")
        request_id = _require_non_empty_str(
            envelope["request_id"], "BridgeResponse.request_id"
        )
        if ok is True:
            if result is None or error is not None:
                raise ValueError(
                    "BridgeResponse ok=true requires result and no error"
                )
            return cls(
                request_id=request_id,
                result=SceneQueryResult.from_dict(
                    _require_exact_mapping(result, "BridgeResponse.result")
                ),
                error=None,
            )
        if error is None or result is not None:
            raise ValueError(
                "BridgeResponse ok=false requires error and no result"
            )
        return cls(
            request_id=request_id,
            result=None,
            error=BridgeError.from_dict(
                _require_exact_mapping(error, "BridgeResponse.error")
            ),
        )


# --- JSON text entrypoints -------------------------------------------------


def parse_request(raw: str | bytes) -> BridgeRequest:
    """Parse a bridge request from strict JSON text.

    Accepts ``str`` or UTF-8 ``bytes``. Enforces the 1 MiB byte limit, valid
    UTF-8, duplicate-key rejection at any depth, then full envelope + payload
    validation via :meth:`BridgeRequest.from_dict`.
    """
    return BridgeRequest.from_dict(
        _require_exact_mapping(_load_strict_json(raw, "Bridge request"), "Bridge request")
    )


def parse_response(raw: str | bytes) -> BridgeResponse:
    """Parse a bridge response from strict JSON text.

    Accepts ``str`` or UTF-8 ``bytes``. Enforces the 1 MiB byte limit, valid
    UTF-8, duplicate-key rejection at any depth, then full response validation
    via :meth:`BridgeResponse.from_dict`.
    """
    return BridgeResponse.from_dict(
        _require_exact_mapping(_load_strict_json(raw, "Bridge response"), "Bridge response")
    )
