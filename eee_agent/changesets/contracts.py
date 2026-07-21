"""Immutable typed ChangeSet / WorkspaceManifest contracts (Task 16-A).

Frozen, slotted, JSON-canonical data transfer objects for the auditable write
boundary: workspace manifests, typed operations, tagged pre/postconditions,
risk/checkpoint summaries, the ChangeSet, approval records, and transactional
receipts, plus the derived :class:`PolicyDecision`.

Strictness mirrors :mod:`eee_agent.runtime.models` and
:mod:`eee_agent.houdini_bridge.contracts`:

* exact primitive types (``bool`` is never an ``int``);
* exact ``dict``/``list``/``tuple`` inputs, deep-frozen at construction;
* canonical JSON via the shared Runtime helpers (sorted keys, compact
  separators, ``ensure_ascii=False``, ``allow_nan=False``) — no second
  permissive JSON implementation;
* aware datetimes normalized to UTC, naive datetimes rejected;
* bounded strings/collections and exact schema versions.

This module imports **neither** ``hou`` **nor** the legacy ``eee_agent.bridge``.
It consumes only the accepted read-only :class:`SceneBinding` DTO from
``eee_agent.houdini_bridge.contracts`` (a pure-JSON contract module).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from eee_agent.core.ids import IdKind, require_id
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# conservative private bounds (design section 4.2)
# --------------------------------------------------------------------------

_MAX_ROOTS = 16
_MAX_NODES = 4096
_MAX_OPERATIONS = 256
_MAX_NODE_REFS = 4096  # combined affected + read-dependency references
_MAX_CHANGESET_BYTES = 128 * 1024
_MAX_PARM_VALUE_BYTES = 16 * 1024
_MAX_IDENTIFIER_LEN = 128  # op_id, node_id, node_name, parm_name, role, capability
_MAX_INSTANCE_ID_LEN = 256
_MAX_NODE_TYPE_LEN = 256
_MAX_NODE_PATH_LEN = 1024
_MAX_SCOPE_IDS = 4096
_MAX_AFFECTED_PATHS = 4096
_MAX_CONDITIONS = 256
_MAX_DETAIL_LEN = 1024

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# namespaced dotted lowercase identifiers (condition tags + denial codes).
_CODE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class PermissionMode(StrEnum):
    """How a ChangeSet is authorized to touch the scene."""

    OWNED_WORKSPACE = "OwnedWorkspace"
    SCOPED_PATCH = "ScopedPatch"
    PROJECT_CHANGE = "ProjectChange"


class Effect(StrEnum):
    """The only forward effects the first Task 16 executor supports."""

    NODE_CREATE = "node.create"
    PARM_SET = "parm.set"
    WIRE_CONNECT = "wire.connect"


class ApprovalDecision(StrEnum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    CONSUMED = "Consumed"
    EXPIRED = "Expired"


class ReceiptStatus(StrEnum):
    APPLIED = "Applied"
    ALREADY_APPLIED = "AlreadyApplied"
    ROLLED_BACK = "RolledBack"
    PARTIAL = "Partial"
    CRITICAL_RECOVERY = "CriticalRecovery"


_VALID_EFFECT_VALUES = frozenset(e.value for e in Effect)


# --------------------------------------------------------------------------
# private primitive validators
# --------------------------------------------------------------------------


def _require_utc(field: str, value: datetime) -> datetime:
    if type(value) is not datetime:
        raise TypeError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _require_exact_bool(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")


def _require_exact_int(value: object, label: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")


def _require_id(value: object, kind: IdKind) -> str:
    if type(value) is not str:
        raise TypeError(f"{kind.value}_ id must be a string")
    return require_id(value, kind)


def _require_sha256(value: object, label: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must contain exactly 64 lowercase hex characters")


def _require_identifier(value: object, label: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a non-empty identifier")
    if len(value) > _MAX_IDENTIFIER_LEN:
        raise ValueError(f"{label} exceeds the maximum identifier length")


def _require_bounded_text(value: object, label: str, max_len: int) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must be a non-empty string")
    if _CONTROL_RE.search(value) is not None:
        raise ValueError(f"{label} must not contain control characters")
    if len(value) > max_len:
        raise ValueError(f"{label} exceeds the maximum length")


def _require_node_path(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute Houdini node path")
    if _CONTROL_RE.search(value) is not None:
        raise ValueError(f"{label} must not contain control characters")
    if len(value) > _MAX_NODE_PATH_LEN:
        raise ValueError(f"{label} exceeds the maximum node path length")


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _freeze_typed_sequence(value: object, label: str, allowed: tuple[type, ...]) -> tuple[object, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    items = tuple(value)
    for item in items:
        if type(item) not in allowed:
            raise TypeError(f"{label} entries must be one of {allowed!r}")
    return items


def _node_identity(ref: NodeRef) -> str:
    return ref.node_id if ref.node_id is not None else ref.path


def _derive_create_path(parent_path: str, node_name: str) -> str:
    """Canonical derived path of a newly created node: parent path + name."""
    return f"{parent_path.rstrip('/')}/{node_name}"


def _unique_node_refs(items: Sequence[NodeRef], label: str) -> None:
    """Reject duplicate or contradictory node references.

    Identity is the stable ``node_id`` when present, otherwise the ``path``.
    Two references that share an identity but disagree on path/type/workspace
    are contradictory, as are two references at the same path with different
    node ids. Exact duplicates are also rejected.
    """
    by_id: dict[str, str] = {}
    by_path: dict[str, str] = {}
    for ref in items:
        canonical = canonical_json_dumps(ref.to_dict())
        if ref.node_id is not None:
            existing = by_id.get(ref.node_id)
            if existing is not None:
                if existing != canonical:
                    raise ValueError(
                        f"{label}: duplicate node id {ref.node_id!r} with conflicting facts"
                    )
                raise ValueError(f"{label}: duplicate node id {ref.node_id!r}")
            by_id[ref.node_id] = canonical
        existing_path = by_path.get(ref.path)
        if existing_path is not None:
            if existing_path != canonical:
                raise ValueError(
                    f"{label}: duplicate node path {ref.path!r} with conflicting facts"
                )
            raise ValueError(f"{label}: duplicate node path {ref.path!r}")
        by_path[ref.path] = canonical


def _normalize_identifiers(
    value: object, label: str, max_count: int
) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    items = tuple(value)
    if len(items) > max_count:
        raise ValueError(f"{label} exceeds the maximum count")
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        _require_identifier(raw, label)
        if raw in seen:
            raise ValueError(f"{label} contains duplicate identifiers")
        seen.add(raw)
        out.append(raw)
    return tuple(out)


def _normalize_paths(value: object, label: str, max_count: int) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    items = tuple(value)
    if len(items) > max_count:
        raise ValueError(f"{label} exceeds the maximum count")
    seen: set[str] = set()
    for raw in items:
        _require_node_path(raw, label)
        if raw in seen:
            raise ValueError(f"{label} contains duplicate paths")
        seen.add(raw)
    return tuple(sorted(seen))


def _normalize_effect_names(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("effect_names must be a sequence")
    seen: set[str] = set()
    for name in value:
        if type(name) is not str or name not in _VALID_EFFECT_VALUES:
            raise ValueError(f"effect_names must be valid Effect values: {name!r}")
        seen.add(name)
    return tuple(sorted(seen))


def _normalize_denial_codes(value: object) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError("denial_codes must be a sequence")
    seen: set[str] = set()
    for code in value:
        if type(code) is not str or _CODE_RE.fullmatch(code) is None:
            raise ValueError(f"denial_codes must be namespaced values: {code!r}")
        seen.add(code)
    return tuple(sorted(seen))


# --------------------------------------------------------------------------
# parameter value contract
# --------------------------------------------------------------------------

_SCALAR_TYPES = (bool, int, float, str)


def _parm_value_size_ok(value: object) -> bool:
    text = canonical_json_dumps(_parm_value_json(value))
    return len(text.encode("utf-8")) <= _MAX_PARM_VALUE_BYTES


def _parm_value_json(value: object) -> object:
    if type(value) is tuple:
        return list(value)
    return value


def _parm_shape(value: object) -> tuple[object, ...]:
    if type(value) is tuple:
        return ("tuple", type(value[0]), len(value))
    return ("scalar", type(value))


def _validate_parm_value(value: object, label: str) -> object:
    value_type = type(value)
    if value_type in (bool, int):
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must be a finite float")
        return value
    if value_type is str:
        if not _parm_value_size_ok(value):
            raise ValueError(f"{label} exceeds the maximum parm value size")
        return value
    if value_type in (tuple, list):
        items = tuple(value)
        if not items:
            raise ValueError(f"{label} tuple must be non-empty")
        element_type: type | None = None
        for item in items:
            item_type = type(item)
            if item_type not in _SCALAR_TYPES:
                raise TypeError(f"{label} tuple elements must be scalars")
            if item_type is float and not math.isfinite(item):
                raise ValueError(f"{label} tuple float must be finite")
            if item_type is str and not _parm_value_size_ok(item):
                raise ValueError(f"{label} exceeds the maximum parm value size")
            if element_type is None:
                element_type = item_type
            elif element_type is not item_type:
                raise ValueError(f"{label} tuple must be homogeneous")
        if not _parm_value_size_ok(items):
            raise ValueError(f"{label} exceeds the maximum parm value size")
        return items
    raise TypeError(f"{label} must be a bounded scalar or homogeneous scalar tuple")


def _require_matching_parm_shape(value: object, old: object, label: str) -> None:
    if _parm_shape(value) != _parm_shape(old):
        raise ValueError(f"{label} value and expected_old_value must share shape and type")


# --------------------------------------------------------------------------
# owned node + node references
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OwnedNodeRef:
    node_id: str
    path: str
    node_type: str
    parent_path: str
    capability: str
    role: str

    def __post_init__(self) -> None:
        _require_identifier(self.node_id, "OwnedNodeRef.node_id")
        _require_node_path(self.path, "OwnedNodeRef.path")
        _require_bounded_text(self.node_type, "OwnedNodeRef.node_type", _MAX_NODE_TYPE_LEN)
        _require_node_path(self.parent_path, "OwnedNodeRef.parent_path")
        _require_identifier(self.capability, "OwnedNodeRef.capability")
        _require_identifier(self.role, "OwnedNodeRef.role")

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "node_type": self.node_type,
            "parent_path": self.parent_path,
            "capability": self.capability,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class NodeRef:
    node_id: str | None
    path: str
    expected_type: str
    expected_workspace_id: str | None

    def __post_init__(self) -> None:
        if self.node_id is not None:
            _require_identifier(self.node_id, "NodeRef.node_id")
        _require_node_path(self.path, "NodeRef.path")
        _require_bounded_text(self.expected_type, "NodeRef.expected_type", _MAX_NODE_TYPE_LEN)
        if self.expected_workspace_id is not None:
            _require_id(self.expected_workspace_id, IdKind.WORKSPACE)

    def to_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "path": self.path,
            "expected_type": self.expected_type,
            "expected_workspace_id": self.expected_workspace_id,
        }


@dataclass(frozen=True, slots=True)
class WireRef:
    source: NodeRef
    source_output_index: int

    def __post_init__(self) -> None:
        if type(self.source) is not NodeRef:
            raise TypeError("WireRef.source must be an exact NodeRef")
        _require_exact_int(self.source_output_index, "WireRef.source_output_index")
        if self.source_output_index < 0:
            raise ValueError("WireRef.source_output_index must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.to_dict(),
            "source_output_index": self.source_output_index,
        }


# --------------------------------------------------------------------------
# typed operations
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreateNode:
    op_id: str
    parent: NodeRef
    node_id: str
    node_type: str
    node_name: str
    workspace_id: str
    capability: str
    role: str

    def __post_init__(self) -> None:
        _require_identifier(self.op_id, "CreateNode.op_id")
        if type(self.parent) is not NodeRef:
            raise TypeError("CreateNode.parent must be an exact NodeRef")
        _require_identifier(self.node_id, "CreateNode.node_id")
        _require_bounded_text(self.node_type, "CreateNode.node_type", _MAX_NODE_TYPE_LEN)
        _require_identifier(self.node_name, "CreateNode.node_name")
        _require_id(self.workspace_id, IdKind.WORKSPACE)
        _require_identifier(self.capability, "CreateNode.capability")
        _require_identifier(self.role, "CreateNode.role")

    @property
    def kind(self) -> str:
        return Effect.NODE_CREATE.value

    @property
    def effect(self) -> Effect:
        return Effect.NODE_CREATE

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "op_id": self.op_id,
            "parent": self.parent.to_dict(),
            "node_id": self.node_id,
            "node_type": self.node_type,
            "node_name": self.node_name,
            "workspace_id": self.workspace_id,
            "capability": self.capability,
            "role": self.role,
        }


@dataclass(frozen=True, slots=True)
class SetParm:
    op_id: str
    target: NodeRef
    parm_name: str
    value: object
    expected_old_value: object

    def __post_init__(self) -> None:
        _require_identifier(self.op_id, "SetParm.op_id")
        if type(self.target) is not NodeRef:
            raise TypeError("SetParm.target must be an exact NodeRef")
        _require_identifier(self.parm_name, "SetParm.parm_name")
        value = _validate_parm_value(self.value, "SetParm.value")
        old = _validate_parm_value(self.expected_old_value, "SetParm.expected_old_value")
        _require_matching_parm_shape(value, old, "SetParm")
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "expected_old_value", old)

    @property
    def kind(self) -> str:
        return Effect.PARM_SET.value

    @property
    def effect(self) -> Effect:
        return Effect.PARM_SET

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "op_id": self.op_id,
            "target": self.target.to_dict(),
            "parm_name": self.parm_name,
            "value": _parm_value_json(self.value),
            "expected_old_value": _parm_value_json(self.expected_old_value),
        }


@dataclass(frozen=True, slots=True)
class ConnectInput:
    op_id: str
    target: NodeRef
    input_index: int
    source: NodeRef
    source_output_index: int
    expected_old_source: WireRef | None

    def __post_init__(self) -> None:
        _require_identifier(self.op_id, "ConnectInput.op_id")
        if type(self.target) is not NodeRef:
            raise TypeError("ConnectInput.target must be an exact NodeRef")
        _require_exact_int(self.input_index, "ConnectInput.input_index")
        if self.input_index < 0:
            raise ValueError("ConnectInput.input_index must be non-negative")
        if type(self.source) is not NodeRef:
            raise TypeError("ConnectInput.source must be an exact NodeRef")
        _require_exact_int(self.source_output_index, "ConnectInput.source_output_index")
        if self.source_output_index < 0:
            raise ValueError("ConnectInput.source_output_index must be non-negative")
        if self.expected_old_source is not None and type(self.expected_old_source) is not WireRef:
            raise TypeError("ConnectInput.expected_old_source must be an exact WireRef or None")

    @property
    def kind(self) -> str:
        return Effect.WIRE_CONNECT.value

    @property
    def effect(self) -> Effect:
        return Effect.WIRE_CONNECT

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "op_id": self.op_id,
            "target": self.target.to_dict(),
            "input_index": self.input_index,
            "source": self.source.to_dict(),
            "source_output_index": self.source_output_index,
            "expected_old_source": (
                self.expected_old_source.to_dict()
                if self.expected_old_source is not None
                else None
            ),
        }


TypedOperation = CreateNode | SetParm | ConnectInput


# --------------------------------------------------------------------------
# tagged preconditions / postconditions (design section 4.3)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SceneBindingEquals:
    instance_id: str
    scene_epoch: int

    def __post_init__(self) -> None:
        _require_bounded_text(self.instance_id, "SceneBindingEquals.instance_id", _MAX_INSTANCE_ID_LEN)
        _require_exact_int(self.scene_epoch, "SceneBindingEquals.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("SceneBindingEquals.scene_epoch must be >= 1")

    @property
    def kind(self) -> str:
        return "scene.binding_equals"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "instance_id": self.instance_id, "scene_epoch": self.scene_epoch}


@dataclass(frozen=True, slots=True)
class WorkspaceRevisionEquals:
    workspace_id: str
    revision: str

    def __post_init__(self) -> None:
        _require_id(self.workspace_id, IdKind.WORKSPACE)
        _require_sha256(self.revision, "WorkspaceRevisionEquals.revision")

    @property
    def kind(self) -> str:
        return "workspace.revision_equals"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "workspace_id": self.workspace_id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class NodeIdentityEquals:
    node: NodeRef

    def __post_init__(self) -> None:
        if type(self.node) is not NodeRef:
            raise TypeError("NodeIdentityEquals.node must be an exact NodeRef")

    @property
    def kind(self) -> str:
        return "node.identity_equals"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "node": self.node.to_dict()}


@dataclass(frozen=True, slots=True)
class ParmValueEquals:
    target: NodeRef
    parm_name: str
    value: object

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("ParmValueEquals.target must be an exact NodeRef")
        _require_identifier(self.parm_name, "ParmValueEquals.parm_name")
        object.__setattr__(self, "value", _validate_parm_value(self.value, "ParmValueEquals.value"))

    @property
    def kind(self) -> str:
        return "parm.value_equals"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target.to_dict(),
            "parm_name": self.parm_name,
            "value": _parm_value_json(self.value),
        }


@dataclass(frozen=True, slots=True)
class WireInputEquals:
    target: NodeRef
    input_index: int
    source: WireRef | None

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("WireInputEquals.target must be an exact NodeRef")
        _require_exact_int(self.input_index, "WireInputEquals.input_index")
        if self.input_index < 0:
            raise ValueError("WireInputEquals.input_index must be non-negative")
        if self.source is not None and type(self.source) is not WireRef:
            raise TypeError("WireInputEquals.source must be an exact WireRef or None")

    @property
    def kind(self) -> str:
        return "wire.input_equals"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "target": self.target.to_dict(),
            "input_index": self.input_index,
            "source": self.source.to_dict() if self.source is not None else None,
        }


@dataclass(frozen=True, slots=True)
class NodeAbsent:
    path: str
    node_id: str

    def __post_init__(self) -> None:
        _require_node_path(self.path, "NodeAbsent.path")
        _require_identifier(self.node_id, "NodeAbsent.node_id")

    @property
    def kind(self) -> str:
        return "node.absent"

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "path": self.path, "node_id": self.node_id}


Precondition = (
    SceneBindingEquals | WorkspaceRevisionEquals | NodeIdentityEquals
    | ParmValueEquals | WireInputEquals | NodeAbsent
)
Postcondition = NodeIdentityEquals | ParmValueEquals | WireInputEquals

_PRECONDITION_TYPES = (
    SceneBindingEquals,
    WorkspaceRevisionEquals,
    NodeIdentityEquals,
    ParmValueEquals,
    WireInputEquals,
    NodeAbsent,
)
_POSTCONDITION_TYPES = (NodeIdentityEquals, ParmValueEquals, WireInputEquals)


def _condition_identity(cond: object) -> tuple[str, ...]:
    if isinstance(cond, SceneBindingEquals):
        return ()
    if isinstance(cond, WorkspaceRevisionEquals):
        return (cond.workspace_id,)
    if isinstance(cond, NodeIdentityEquals):
        return (_node_identity(cond.node),)
    if isinstance(cond, ParmValueEquals):
        return (_node_identity(cond.target), cond.parm_name)
    if isinstance(cond, WireInputEquals):
        return (_node_identity(cond.target), str(cond.input_index))
    if isinstance(cond, NodeAbsent):
        return (cond.path, cond.node_id)
    raise TypeError(f"unsupported condition type: {type(cond)!r}")


def _freeze_conditions(
    value: object, label: str, allowed: tuple[type, ...]
) -> tuple[object, ...]:
    items = _freeze_typed_sequence(value, label, allowed)
    if len(items) > _MAX_CONDITIONS:
        raise ValueError(f"{label} exceeds the maximum count")
    seen: dict[tuple[str, ...], str] = {}
    absent_paths: set[str] = set()
    present_paths: set[str] = set()
    for cond in items:
        key = (cond.kind,) + _condition_identity(cond)  # type: ignore[attr-defined]
        payload = canonical_json_dumps(cond.to_dict())  # type: ignore[attr-defined]
        if key in seen:
            if seen[key] != payload:
                raise ValueError(f"{label} contains contradictory {cond.kind}")  # type: ignore[attr-defined]
            raise ValueError(f"{label} contains duplicate {cond.kind}")  # type: ignore[attr-defined]
        seen[key] = payload
        if isinstance(cond, NodeAbsent):
            absent_paths.add(cond.path)
        if isinstance(cond, NodeIdentityEquals):
            present_paths.add(cond.node.path)
    if absent_paths & present_paths:
        raise ValueError(f"{label} contains contradictory node absent/present facts")
    return items


# --------------------------------------------------------------------------
# risk + checkpoint summaries
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskSummary:
    touches_external_nodes: bool
    changes_wiring: bool
    requires_backup: bool
    operation_count: int
    effect_names: tuple[str, ...]
    affected_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_exact_bool(self.touches_external_nodes, "RiskSummary.touches_external_nodes")
        _require_exact_bool(self.changes_wiring, "RiskSummary.changes_wiring")
        _require_exact_bool(self.requires_backup, "RiskSummary.requires_backup")
        _require_exact_int(self.operation_count, "RiskSummary.operation_count")
        if self.operation_count < 0 or self.operation_count > _MAX_OPERATIONS:
            raise ValueError("RiskSummary.operation_count must be within the operation limit")
        object.__setattr__(self, "effect_names", _normalize_effect_names(self.effect_names))
        paths = _normalize_paths(self.affected_paths, "RiskSummary.affected_paths", _MAX_AFFECTED_PATHS)
        object.__setattr__(self, "affected_paths", paths)

    def to_dict(self) -> dict[str, object]:
        return {
            "touches_external_nodes": self.touches_external_nodes,
            "changes_wiring": self.changes_wiring,
            "requires_backup": self.requires_backup,
            "operation_count": self.operation_count,
            "effect_names": list(self.effect_names),
            "affected_paths": list(self.affected_paths),
        }


@dataclass(frozen=True, slots=True)
class ParmSnapshot:
    target: NodeRef
    parm_name: str

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("ParmSnapshot.target must be an exact NodeRef")
        _require_identifier(self.parm_name, "ParmSnapshot.parm_name")

    def to_dict(self) -> dict[str, object]:
        return {"target": self.target.to_dict(), "parm_name": self.parm_name}


@dataclass(frozen=True, slots=True)
class WireSnapshot:
    target: NodeRef
    input_index: int

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("WireSnapshot.target must be an exact NodeRef")
        _require_exact_int(self.input_index, "WireSnapshot.input_index")
        if self.input_index < 0:
            raise ValueError("WireSnapshot.input_index must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {"target": self.target.to_dict(), "input_index": self.input_index}


@dataclass(frozen=True, slots=True)
class CheckpointPlan:
    nodes: tuple[NodeRef, ...]
    parameters: tuple[ParmSnapshot, ...]
    wires: tuple[WireSnapshot, ...]

    def __post_init__(self) -> None:
        nodes = _freeze_typed_sequence(self.nodes, "CheckpointPlan.nodes", (NodeRef,))
        parameters = _freeze_typed_sequence(self.parameters, "CheckpointPlan.parameters", (ParmSnapshot,))
        wires = _freeze_typed_sequence(self.wires, "CheckpointPlan.wires", (WireSnapshot,))
        for collection, label in (
            (nodes, "CheckpointPlan.nodes"),
            (parameters, "CheckpointPlan.parameters"),
            (wires, "CheckpointPlan.wires"),
        ):
            if len(collection) > _MAX_NODE_REFS:
                raise ValueError(f"{label} exceeds the maximum count")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "wires", wires)

    def to_dict(self) -> dict[str, object]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "parameters": [p.to_dict() for p in self.parameters],
            "wires": [w.to_dict() for w in self.wires],
        }


# --------------------------------------------------------------------------
# WorkspaceManifest
# --------------------------------------------------------------------------


def _freeze_owned(
    value: object, label: str, max_count: int
) -> tuple[OwnedNodeRef, ...]:
    items = _freeze_typed_sequence(value, label, (OwnedNodeRef,))
    if len(items) > max_count:
        raise ValueError(f"{label} exceeds the maximum count")
    return items  # type: ignore[return-value]


def _validate_manifest_structure(
    roots: tuple[OwnedNodeRef, ...], nodes: tuple[OwnedNodeRef, ...]
) -> None:
    seen_node_ids: set[str] = set()
    seen_paths: set[str] = set()
    for node in nodes:
        if node.node_id in seen_node_ids:
            raise ValueError("WorkspaceManifest.nodes contains a duplicate node id")
        if node.path in seen_paths:
            raise ValueError("WorkspaceManifest.nodes contains a duplicate node path")
        seen_node_ids.add(node.node_id)
        seen_paths.add(node.path)
    root_ids: set[str] = set()
    root_paths: set[str] = set()
    for root in roots:
        if root.node_id in root_ids or root.path in root_paths:
            raise ValueError("WorkspaceManifest.roots contains a duplicate root")
        root_ids.add(root.node_id)
        root_paths.add(root.path)
    node_set = set(nodes)
    for root in roots:
        if root not in node_set:
            raise ValueError(
                "WorkspaceManifest every root must be present in nodes with identical facts"
            )


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    workspace_id: str
    session_id: str
    instance_id: str
    scene_epoch: int
    revision: str
    roots: tuple[OwnedNodeRef, ...]
    nodes: tuple[OwnedNodeRef, ...]
    created_by_run: str
    updated_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("WorkspaceManifest.schema_version must be the supported value 1")
        _require_id(self.workspace_id, IdKind.WORKSPACE)
        _require_id(self.session_id, IdKind.SESSION)
        _require_bounded_text(self.instance_id, "WorkspaceManifest.instance_id", _MAX_INSTANCE_ID_LEN)
        _require_exact_int(self.scene_epoch, "WorkspaceManifest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("WorkspaceManifest.scene_epoch must be >= 1")
        _require_sha256(self.revision, "WorkspaceManifest.revision")
        roots = _freeze_owned(self.roots, "WorkspaceManifest.roots", _MAX_ROOTS)
        nodes = _freeze_owned(self.nodes, "WorkspaceManifest.nodes", _MAX_NODES)
        if not roots:
            raise ValueError("WorkspaceManifest.roots must contain at least one root")
        if not nodes:
            raise ValueError("WorkspaceManifest.nodes must contain at least one node")
        _require_id(self.created_by_run, IdKind.RUN)
        object.__setattr__(self, "updated_at", _require_utc("WorkspaceManifest.updated_at", self.updated_at))
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "nodes", nodes)
        _validate_manifest_structure(roots, nodes)
        expected = type(self).compute_revision(
            self.workspace_id,
            self.session_id,
            self.instance_id,
            self.scene_epoch,
            self.created_by_run,
            roots,
            nodes,
        )
        if expected != self.revision:
            raise ValueError("WorkspaceManifest.revision does not match the canonical identity hash")

    @staticmethod
    def _identity_payload(
        workspace_id: str,
        session_id: str,
        instance_id: str,
        scene_epoch: int,
        created_by_run: str,
        roots: Sequence[OwnedNodeRef],
        nodes: Sequence[OwnedNodeRef],
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "instance_id": instance_id,
            "scene_epoch": scene_epoch,
            "created_by_run": created_by_run,
            "roots": [r.to_dict() for r in roots],
            "nodes": [n.to_dict() for n in nodes],
        }

    @classmethod
    def compute_revision(
        cls,
        workspace_id: str,
        session_id: str,
        instance_id: str,
        scene_epoch: int,
        created_by_run: str,
        roots: Sequence[OwnedNodeRef],
        nodes: Sequence[OwnedNodeRef],
    ) -> str:
        """Deterministic canonical SHA-256 of manifest identity (excludes updated_at/revision)."""
        payload = cls._identity_payload(
            workspace_id, session_id, instance_id, scene_epoch, created_by_run, roots, nodes
        )
        return _sha256_hex(canonical_json_dumps(payload))

    @classmethod
    def build(
        cls,
        workspace_id: str,
        session_id: str,
        instance_id: str,
        scene_epoch: int,
        roots: Sequence[OwnedNodeRef],
        nodes: Sequence[OwnedNodeRef],
        created_by_run: str,
        updated_at: datetime,
    ) -> WorkspaceManifest:
        revision = cls.compute_revision(
            workspace_id, session_id, instance_id, scene_epoch, created_by_run, roots, nodes
        )
        return cls(
            workspace_id=workspace_id,
            session_id=session_id,
            instance_id=instance_id,
            scene_epoch=scene_epoch,
            revision=revision,
            roots=roots,
            nodes=nodes,
            created_by_run=created_by_run,
            updated_at=updated_at,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "instance_id": self.instance_id,
            "scene_epoch": self.scene_epoch,
            "revision": self.revision,
            "roots": [r.to_dict() for r in self.roots],
            "nodes": [n.to_dict() for n in self.nodes],
            "created_by_run": self.created_by_run,
            "updated_at": self.updated_at.isoformat(),
        }


# --------------------------------------------------------------------------
# condition result + ChangeSet
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConditionResult:
    kind: str
    passed: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or _CODE_RE.fullmatch(self.kind) is None:
            raise ValueError("ConditionResult.kind must be a namespaced condition tag")
        _require_exact_bool(self.passed, "ConditionResult.passed")
        if self.detail is not None:
            _require_bounded_text(self.detail, "ConditionResult.detail", _MAX_DETAIL_LEN)

    def to_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "passed": self.passed, "detail": self.detail}


def _freeze_operations(value: object) -> tuple[object, ...]:
    items = _freeze_typed_sequence(value, "ChangeSet.operations", (CreateNode, SetParm, ConnectInput))
    if not items:
        raise ValueError("ChangeSet.operations must contain at least one operation")
    if len(items) > _MAX_OPERATIONS:
        raise ValueError("ChangeSet.operations exceeds the maximum count")
    seen: set[str] = set()
    for op in items:
        if op.op_id in seen:  # type: ignore[attr-defined]
            raise ValueError("ChangeSet.operations contains a duplicate op_id")
        seen.add(op.op_id)  # type: ignore[attr-defined]
    return items


def _check_unique_created_nodes(items: Sequence[object]) -> None:
    """Reject repeated created node ids or derived create paths.

    Distinct operation ids must not make a ``node.create`` effect repeat the
    same stable node id or the same derived path within one ChangeSet.
    """
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for op in items:
        if not isinstance(op, CreateNode):
            continue
        if op.node_id in seen_ids:
            raise ValueError("ChangeSet.operations: duplicate created node id")
        seen_ids.add(op.node_id)
        derived = _derive_create_path(op.parent.path, op.node_name)
        if derived in seen_paths:
            raise ValueError("ChangeSet.operations: duplicate created node path")
        seen_paths.add(derived)


# --------------------------------------------------------------------------
# D1: intra-ChangeSet created-reference forward-sequence validation
# --------------------------------------------------------------------------


def _op_node_refs(op: object) -> list[NodeRef]:
    """The NodeRefs an operation references (parent / target / source + old source)."""
    if isinstance(op, CreateNode):
        return [op.parent]
    if isinstance(op, SetParm):
        return [op.target]
    if isinstance(op, ConnectInput):
        refs = [op.target, op.source]
        if op.expected_old_source is not None:
            refs.append(op.expected_old_source.source)
        return refs
    return []


def _condition_refs(cond: object) -> list[NodeRef]:
    """Extract every NodeRef embedded in a condition."""
    if isinstance(cond, NodeIdentityEquals):
        return [cond.node]
    if isinstance(cond, ParmValueEquals):
        return [cond.target]
    if isinstance(cond, WireInputEquals):
        refs = [cond.target]
        if cond.source is not None:
            refs.append(cond.source.source)
        return refs
    return []


def _is_created_ref(ref: NodeRef, created_ids: set[str], created_paths: set[str]) -> bool:
    """Whether a NodeRef collides with any declared create by ID or path."""
    return (ref.node_id is not None and ref.node_id in created_ids) or ref.path in created_paths


def _check_created_ref(
    ref: NodeRef,
    current_index: int,
    created_by_id: dict[str, tuple[NodeRef, int]],
    created_by_path: dict[str, tuple[NodeRef, int]],
) -> None:
    """Reject a reference that collides with a created identity but does not
    exactly match its derived NodeRef, or whose producer is not earlier."""
    if ref.node_id is not None and ref.node_id in created_by_id:
        derived, producer_index = created_by_id[ref.node_id]
        if ref != derived:
            raise ValueError(
                "ChangeSet.operations: created node id paired with different facts"
            )
        if producer_index >= current_index:
            raise ValueError(
                "ChangeSet.operations: forward reference to a later created node"
            )
        return
    if ref.path in created_by_path:
        derived, producer_index = created_by_path[ref.path]
        if ref != derived:
            raise ValueError(
                "ChangeSet.operations: created node path paired with different facts"
            )
        if producer_index >= current_index:
            raise ValueError(
                "ChangeSet.operations: forward reference to a later created node"
            )


def _reject_impossible_preflight_facts(
    preconditions: Sequence[object],
    checkpoint_plan: "CheckpointPlan",
    created_ids: set[str],
    created_paths: set[str],
) -> None:
    """Reject preconditions/checkpoints that require the state of a node that is
    only created during this transaction — they cannot be verified at preflight."""
    for cond in preconditions:
        if isinstance(cond, (NodeIdentityEquals, ParmValueEquals, WireInputEquals)):
            for ref in _condition_refs(cond):
                if _is_created_ref(ref, created_ids, created_paths):
                    raise ValueError(
                        "ChangeSet: precondition references a transaction-created "
                        "node whose state cannot exist at preflight"
                    )
    for snap in checkpoint_plan.parameters:
        if _is_created_ref(snap.target, created_ids, created_paths):
            raise ValueError(
                "ChangeSet: checkpoint parameter targets a transaction-created node"
            )
    for snap in checkpoint_plan.wires:
        if _is_created_ref(snap.target, created_ids, created_paths):
            raise ValueError(
                "ChangeSet: checkpoint wire targets a transaction-created node"
            )
    for ref in checkpoint_plan.nodes:
        if _is_created_ref(ref, created_ids, created_paths):
            raise ValueError(
                "ChangeSet: checkpoint node targets a transaction-created node"
            )


def _validate_created_references(
    items: Sequence[object],
    affected: Sequence[NodeRef],
    read_deps: Sequence[NodeRef],
    preconditions: Sequence[object],
    expected_postconditions: Sequence[object],
    checkpoint_plan: "CheckpointPlan",
) -> None:
    """Validate the forward-sequence rule for created-node references (D1).

    Collects every declared create's derived NodeRef and op index, then checks
    EVERY embedded NodeRef surface: operation refs (including expected_old_source),
    affected nodes, read dependencies, preconditions, postconditions (including
    WireRef.source), and checkpoint entries. Ordered operation refs must point to
    an earlier producer; aggregate refs require exact derived identity.

    Preconditions/checkpoints that require the state of a transaction-created
    node are impossible at preflight and fail closed at construction.
    """
    created_by_id: dict[str, tuple[NodeRef, int]] = {}
    created_by_path: dict[str, tuple[NodeRef, int]] = {}
    for index, op in enumerate(items):
        if isinstance(op, CreateNode):
            derived_path = _derive_create_path(op.parent.path, op.node_name)
            ref = NodeRef(
                node_id=op.node_id,
                path=derived_path,
                expected_type=op.node_type,
                expected_workspace_id=op.workspace_id,
            )
            created_by_id[op.node_id] = (ref, index)
            created_by_path[derived_path] = (ref, index)
    created_ids = set(created_by_id)
    created_paths = set(created_by_path)
    total = len(items)

    # 1. Ordered operation references (forward-reference check applies).
    for index, op in enumerate(items):
        for ref in _op_node_refs(op):
            _check_created_ref(ref, index, created_by_id, created_by_path)

    # 2. Aggregate references — exact match only (all creates are "earlier").
    for ref in list(affected) + list(read_deps):
        _check_created_ref(ref, total, created_by_id, created_by_path)

    # 3. Condition references (pre + post) — exact match only.
    for cond in list(preconditions) + list(expected_postconditions):
        for ref in _condition_refs(cond):
            _check_created_ref(ref, total, created_by_id, created_by_path)

    # 4. Checkpoint references — exact match only.
    for ref in list(checkpoint_plan.nodes):
        _check_created_ref(ref, total, created_by_id, created_by_path)
    for snap in list(checkpoint_plan.parameters):
        _check_created_ref(snap.target, total, created_by_id, created_by_path)
    for snap in list(checkpoint_plan.wires):
        _check_created_ref(snap.target, total, created_by_id, created_by_path)

    # 5. Reject impossible preflight facts (created-node state in pre/checkpoint).
    _reject_impossible_preflight_facts(preconditions, checkpoint_plan, created_ids, created_paths)


def _freeze_node_refs(value: object, label: str) -> tuple[NodeRef, ...]:
    return _freeze_typed_sequence(value, label, (NodeRef,))  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ChangeSet:
    change_id: str
    session_id: str
    run_id: str
    scene_binding: SceneBinding
    workspace_id: str | None
    base_revision: str
    required_permission: PermissionMode
    scoped_node_ids: tuple[str, ...]
    operations: tuple[TypedOperation, ...]
    affected_nodes: tuple[NodeRef, ...]
    read_dependencies: tuple[NodeRef, ...]
    preconditions: tuple[Precondition, ...]
    expected_postconditions: tuple[Postcondition, ...]
    risk_summary: RiskSummary
    checkpoint_plan: CheckpointPlan
    created_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ChangeSet.schema_version must be the supported value 1")
        _require_id(self.change_id, IdKind.CHANGE)
        _require_id(self.session_id, IdKind.SESSION)
        _require_id(self.run_id, IdKind.RUN)
        if type(self.scene_binding) is not SceneBinding:
            raise TypeError("ChangeSet.scene_binding must be an exact SceneBinding")
        if self.workspace_id is not None:
            _require_id(self.workspace_id, IdKind.WORKSPACE)
        _require_sha256(self.base_revision, "ChangeSet.base_revision")
        if type(self.required_permission) is not PermissionMode:
            raise ValueError("ChangeSet.required_permission must be an exact PermissionMode")
        object.__setattr__(
            self,
            "scoped_node_ids",
            _normalize_identifiers(self.scoped_node_ids, "ChangeSet.scoped_node_ids", _MAX_SCOPE_IDS),
        )
        operations = _freeze_operations(self.operations)
        _check_unique_created_nodes(operations)
        affected = _freeze_node_refs(self.affected_nodes, "ChangeSet.affected_nodes")
        read_deps = _freeze_node_refs(self.read_dependencies, "ChangeSet.read_dependencies")
        if len(affected) + len(read_deps) > _MAX_NODE_REFS:
            raise ValueError("ChangeSet affected/read-dependency references exceed the limit")
        _unique_node_refs(affected, "ChangeSet.affected_nodes")
        _unique_node_refs(read_deps, "ChangeSet.read_dependencies")
        preconditions = _freeze_conditions(self.preconditions, "ChangeSet.preconditions", _PRECONDITION_TYPES)
        postconditions = _freeze_conditions(
            self.expected_postconditions, "ChangeSet.expected_postconditions", _POSTCONDITION_TYPES
        )
        if type(self.risk_summary) is not RiskSummary:
            raise TypeError("ChangeSet.risk_summary must be an exact RiskSummary")
        if type(self.checkpoint_plan) is not CheckpointPlan:
            raise TypeError("ChangeSet.checkpoint_plan must be an exact CheckpointPlan")
        _validate_created_references(
            operations, affected, read_deps, preconditions, postconditions,
            self.checkpoint_plan,
        )
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "affected_nodes", affected)
        object.__setattr__(self, "read_dependencies", read_deps)
        object.__setattr__(self, "preconditions", preconditions)
        object.__setattr__(self, "expected_postconditions", postconditions)
        object.__setattr__(self, "created_at", _require_utc("ChangeSet.created_at", self.created_at))
        payload_size = len(canonical_json_dumps(self.to_dict()).encode("utf-8"))
        if payload_size > _MAX_CHANGESET_BYTES:
            raise ValueError("ChangeSet canonical payload exceeds the maximum size")

    @property
    def digest(self) -> str:
        return _sha256_hex(canonical_json_dumps(self.to_dict()))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "change_id": self.change_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "scene_binding": self.scene_binding.to_dict(),
            "workspace_id": self.workspace_id,
            "base_revision": self.base_revision,
            "required_permission": self.required_permission.value,
            "scoped_node_ids": list(self.scoped_node_ids),
            "operations": [op.to_dict() for op in self.operations],
            "affected_nodes": [n.to_dict() for n in self.affected_nodes],
            "read_dependencies": [n.to_dict() for n in self.read_dependencies],
            "preconditions": [c.to_dict() for c in self.preconditions],
            "expected_postconditions": [c.to_dict() for c in self.expected_postconditions],
            "risk_summary": self.risk_summary.to_dict(),
            "checkpoint_plan": self.checkpoint_plan.to_dict(),
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------
# ApprovalRecord
# --------------------------------------------------------------------------


def _validate_approval_state(record: ApprovalRecord) -> None:
    decision = record.decision
    if decision is ApprovalDecision.PENDING:
        if record.decided_at is not None or record.decided_by is not None:
            raise ValueError("ApprovalRecord PENDING must not carry decision facts")
        if record.approved_instance_id is not None or record.approved_scene_epoch is not None:
            raise ValueError("ApprovalRecord PENDING must not bind instance/epoch")
    elif decision in (ApprovalDecision.APPROVED, ApprovalDecision.CONSUMED):
        if record.decided_at is None or record.decided_by != "local_user":
            raise ValueError(
                "ApprovalRecord APPROVED/CONSUMED require decided_at and decided_by"
            )
        if record.approved_instance_id is None or record.approved_scene_epoch is None:
            raise ValueError("ApprovalRecord APPROVED/CONSUMED must bind instance/epoch")
        _require_bounded_text(
            record.approved_instance_id, "ApprovalRecord.approved_instance_id", _MAX_INSTANCE_ID_LEN
        )
        _require_exact_int(record.approved_scene_epoch, "ApprovalRecord.approved_scene_epoch")
        if record.approved_scene_epoch < 1:
            raise ValueError("ApprovalRecord.approved_scene_epoch must be >= 1")
    elif decision is ApprovalDecision.REJECTED:
        if record.decided_at is None or record.decided_by != "local_user":
            raise ValueError("ApprovalRecord REJECTED requires decided_at and decided_by")
        if record.approved_instance_id is not None or record.approved_scene_epoch is not None:
            raise ValueError("ApprovalRecord REJECTED must not bind instance/epoch")
    elif decision is ApprovalDecision.EXPIRED:
        if record.decided_at is None:
            raise ValueError("ApprovalRecord EXPIRED requires decided_at")
        if record.decided_by is not None:
            raise ValueError("ApprovalRecord EXPIRED must not be attributed to a user")
        if record.approved_instance_id is not None or record.approved_scene_epoch is not None:
            raise ValueError("ApprovalRecord EXPIRED must not bind instance/epoch")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    change_id: str
    changeset_digest: str
    decision: ApprovalDecision
    decided_by: str | None
    requested_at: datetime
    decided_at: datetime | None
    expires_at: datetime
    approved_instance_id: str | None
    approved_scene_epoch: int | None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ApprovalRecord.schema_version must be the supported value 1")
        _require_id(self.approval_id, IdKind.APPROVAL)
        _require_id(self.change_id, IdKind.CHANGE)
        _require_sha256(self.changeset_digest, "ApprovalRecord.changeset_digest")
        if type(self.decision) is not ApprovalDecision:
            raise ValueError("ApprovalRecord.decision must be an exact ApprovalDecision")
        if self.decided_by is not None and self.decided_by != "local_user":
            raise ValueError("ApprovalRecord.decided_by must be 'local_user' or None")
        object.__setattr__(self, "requested_at", _require_utc("ApprovalRecord.requested_at", self.requested_at))
        object.__setattr__(self, "expires_at", _require_utc("ApprovalRecord.expires_at", self.expires_at))
        if self.expires_at <= self.requested_at:
            raise ValueError("ApprovalRecord.expires_at must be after requested_at")
        if self.decided_at is not None:
            object.__setattr__(self, "decided_at", _require_utc("ApprovalRecord.decided_at", self.decided_at))
        _validate_approval_state(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "change_id": self.change_id,
            "changeset_digest": self.changeset_digest,
            "decision": self.decision.value,
            "decided_by": self.decided_by,
            "requested_at": self.requested_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at is not None else None,
            "expires_at": self.expires_at.isoformat(),
            "approved_instance_id": self.approved_instance_id,
            "approved_scene_epoch": self.approved_scene_epoch,
        }


# --------------------------------------------------------------------------
# ChangeReceipt
# --------------------------------------------------------------------------


def _freeze_results(value: object, label: str) -> tuple[ConditionResult, ...]:
    items = _freeze_typed_sequence(value, label, (ConditionResult,))
    if len(items) > _MAX_CONDITIONS:
        raise ValueError(f"{label} exceeds the maximum count")
    return items  # type: ignore[return-value]


def _validate_receipt_state(receipt: ChangeReceipt) -> None:
    status = receipt.status
    if status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED):
        if receipt.scene_may_have_changed:
            raise ValueError("Applied/AlreadyApplied receipts require scene_may_have_changed=False")
        if any(not result.passed for result in receipt.postcondition_results):
            raise ValueError("Applied/AlreadyApplied receipts require every postcondition to pass")
    elif status is ReceiptStatus.ROLLED_BACK:
        if any(not result.passed for result in receipt.rollback_results):
            raise ValueError("RolledBack receipts require every rollback result to pass")
    elif status in (ReceiptStatus.PARTIAL, ReceiptStatus.CRITICAL_RECOVERY):
        if not receipt.scene_may_have_changed:
            raise ValueError("Partial/CriticalRecovery receipts require scene_may_have_changed=True")


@dataclass(frozen=True, slots=True)
class ChangeReceipt:
    change_id: str
    status: ReceiptStatus
    instance_id: str
    scene_epoch: int
    before_revision: str
    after_revision: str
    applied_op_ids: tuple[str, ...]
    postcondition_results: tuple[ConditionResult, ...]
    rollback_results: tuple[ConditionResult, ...]
    scene_may_have_changed: bool
    completed_at: datetime
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ChangeReceipt.schema_version must be the supported value 1")
        _require_id(self.change_id, IdKind.CHANGE)
        if type(self.status) is not ReceiptStatus:
            raise ValueError("ChangeReceipt.status must be an exact ReceiptStatus")
        _require_bounded_text(self.instance_id, "ChangeReceipt.instance_id", _MAX_INSTANCE_ID_LEN)
        _require_exact_int(self.scene_epoch, "ChangeReceipt.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("ChangeReceipt.scene_epoch must be >= 1")
        _require_sha256(self.before_revision, "ChangeReceipt.before_revision")
        _require_sha256(self.after_revision, "ChangeReceipt.after_revision")
        object.__setattr__(
            self,
            "applied_op_ids",
            _normalize_identifiers(self.applied_op_ids, "ChangeReceipt.applied_op_ids", _MAX_OPERATIONS),
        )
        post = _freeze_results(self.postcondition_results, "ChangeReceipt.postcondition_results")
        rollback = _freeze_results(self.rollback_results, "ChangeReceipt.rollback_results")
        _require_exact_bool(self.scene_may_have_changed, "ChangeReceipt.scene_may_have_changed")
        object.__setattr__(self, "postcondition_results", post)
        object.__setattr__(self, "rollback_results", rollback)
        object.__setattr__(self, "completed_at", _require_utc("ChangeReceipt.completed_at", self.completed_at))
        _validate_receipt_state(self)

    @property
    def is_success(self) -> bool:
        return self.status in (ReceiptStatus.APPLIED, ReceiptStatus.ALREADY_APPLIED)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "change_id": self.change_id,
            "status": self.status.value,
            "instance_id": self.instance_id,
            "scene_epoch": self.scene_epoch,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "applied_op_ids": list(self.applied_op_ids),
            "postcondition_results": [r.to_dict() for r in self.postcondition_results],
            "rollback_results": [r.to_dict() for r in self.rollback_results],
            "scene_may_have_changed": self.scene_may_have_changed,
            "completed_at": self.completed_at.isoformat(),
        }


# --------------------------------------------------------------------------
# PolicyDecision (derived; no schema version)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    mode: PermissionMode
    normalized_effects: tuple[str, ...]
    approval_required: bool
    backup_required: bool
    denial_codes: tuple[str, ...]
    changeset_digest: str

    def __post_init__(self) -> None:
        _require_exact_bool(self.allowed, "PolicyDecision.allowed")
        if type(self.mode) is not PermissionMode:
            raise ValueError("PolicyDecision.mode must be an exact PermissionMode")
        object.__setattr__(self, "normalized_effects", _normalize_effect_names(self.normalized_effects))
        _require_exact_bool(self.approval_required, "PolicyDecision.approval_required")
        if not self.approval_required:
            raise ValueError("PolicyDecision.approval_required must be True")
        _require_exact_bool(self.backup_required, "PolicyDecision.backup_required")
        object.__setattr__(self, "denial_codes", _normalize_denial_codes(self.denial_codes))
        _require_sha256(self.changeset_digest, "PolicyDecision.changeset_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "mode": self.mode.value,
            "normalized_effects": list(self.normalized_effects),
            "approval_required": self.approval_required,
            "backup_required": self.backup_required,
            "denial_codes": list(self.denial_codes),
            "changeset_digest": self.changeset_digest,
        }
