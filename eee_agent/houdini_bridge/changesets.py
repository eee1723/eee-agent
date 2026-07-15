"""Additive ``changeset.v1`` Bridge capability and typed preflight DTOs.

This module adds the smallest auditable read-only ChangeSet surface on top of
the accepted :mod:`eee_agent.houdini_bridge.contracts` (Task 15) and the
immutable Task 16-A contracts (:mod:`eee_agent.changesets.contracts`). It
defines:

* the advertised :data:`CHANGESET_V1` capability and
  :func:`validate_capabilities`;
* frozen, slotted, JSON-canonical DTOs for the ``changeset.preflight`` request
  and its bounded typed fact response; and
* strict parsers (:func:`parse_preflight_request` / :func:`parse_preflight_response`).

It imports **neither** ``hou`` **nor** ``rpyc`` and depends only on the accepted
JSON-canonical helpers and the immutable ChangeSet/manifest contracts. The
ChangeSet/manifest deserializers mirror the accepted repository decoders but are
self-contained here so the Bridge layer never imports the persistence layer.

Strictness mirrors the rest of the bridge package: exact primitive types, exact
field sets, deep-frozen canonical JSON, duplicate-key rejection, finite numbers,
and an explicit size limit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from eee_agent.changesets.contracts import (
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmSnapshot,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
    _parm_value_json,
    _validate_parm_value,
)
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    SceneBinding,
)
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

CHANGESET_V1 = "changeset.v1"
OPERATION = "changeset.preflight"

_MAX_REQUEST_ID_LEN = 128
_MIN_DEADLINE_MS = 1
_MAX_DEADLINE_MS = 30_000
_MAX_RESULT_BYTES = 256 * 1024
_MAX_NODE_PATH_LEN = 1024
_MAX_NODE_TYPE_LEN = 256
_MAX_IDENTIFIER_LEN = 128

_CAPABILITY_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f]")

# exact field sets for envelope + payload validation
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
_REQUEST_PAYLOAD_FIELDS = frozenset({"changeset", "changeset_digest", "workspace"})
_RESULT_FIELDS = frozenset(
    {
        "binding",
        "workspace_id",
        "workspace_revision",
        "node_facts",
        "parm_facts",
        "wire_facts",
        "condition_results",
        "all_preconditions_hold",
        "scene_may_have_changed",
    }
)
_NODE_FACT_FIELDS = frozenset(
    {
        "requested",
        "exists",
        "actual_path",
        "actual_type",
        "parent_path",
        "workspace_id",
        "node_id",
        "capability",
        "role",
        "is_locked",
    }
)
_PARM_FACT_FIELDS = frozenset({"target", "parm_name", "exists", "value"})
_WIRE_FACT_FIELDS = frozenset({"target", "input_index", "source"})
_CONDITION_RESULT_FIELDS = frozenset({"kind", "passed", "detail"})
_RESPONSE_REQUIRED_FIELDS = frozenset({"protocol", "kind", "request_id", "ok"})

# ChangeSet/manifest deserializer field sets (mirror the accepted repository).
_CHANGESET_FIELDS = frozenset(
    {
        "schema_version",
        "change_id",
        "session_id",
        "run_id",
        "scene_binding",
        "workspace_id",
        "base_revision",
        "required_permission",
        "scoped_node_ids",
        "operations",
        "affected_nodes",
        "read_dependencies",
        "preconditions",
        "expected_postconditions",
        "risk_summary",
        "checkpoint_plan",
        "created_at",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "workspace_id",
        "session_id",
        "instance_id",
        "scene_epoch",
        "revision",
        "roots",
        "nodes",
        "created_by_run",
        "updated_at",
    }
)
_OWNED_FIELDS = frozenset(
    {"node_id", "path", "node_type", "parent_path", "capability", "role"}
)
_NODEREF_FIELDS = frozenset(
    {"node_id", "path", "expected_type", "expected_workspace_id"}
)
_WIREREF_FIELDS = frozenset({"source", "source_output_index"})
_CREATE_FIELDS = frozenset(
    {
        "kind",
        "op_id",
        "parent",
        "node_id",
        "node_type",
        "node_name",
        "workspace_id",
        "capability",
        "role",
    }
)
_SETPARM_FIELDS = frozenset(
    {"kind", "op_id", "target", "parm_name", "value", "expected_old_value"}
)
_CONNECT_FIELDS = frozenset(
    {
        "kind",
        "op_id",
        "target",
        "input_index",
        "source",
        "source_output_index",
        "expected_old_source",
    }
)
_RISK_FIELDS = frozenset(
    {
        "touches_external_nodes",
        "changes_wiring",
        "requires_backup",
        "operation_count",
        "effect_names",
        "affected_paths",
    }
)
_CHECKPOINT_FIELDS = frozenset({"nodes", "parameters", "wires"})
_PARM_SNAPSHOT_FIELDS = frozenset({"target", "parm_name"})
_WIRE_SNAPSHOT_FIELDS = frozenset({"target", "input_index"})


# --------------------------------------------------------------------------
# capability negotiation
# --------------------------------------------------------------------------


def validate_capabilities(value: object) -> tuple[str, ...]:
    """Validate a hello-ack ``capabilities`` list into a sorted unique tuple.

    The accepted form is a list of non-empty, namespaced lowercase strings
    (``a.b`` style) with no duplicates, sorted ascending. ``[]`` is accepted as
    an empty capability set. Any non-list, non-string element, bad grammar,
    duplicate, or unsorted value raises ``TypeError``/``ValueError`` so the
    client fails closed on an untrustworthy advertisement.
    """
    if type(value) is not list:
        raise TypeError("capabilities must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for cap in value:
        if type(cap) is not str:
            raise TypeError("capabilities entries must be exact strings")
        if _CAPABILITY_RE.fullmatch(cap) is None:
            raise ValueError(f"capability {cap!r} is not a valid namespaced identifier")
        if cap in seen:
            raise ValueError(f"capabilities contains a duplicate: {cap!r}")
        seen.add(cap)
        out.append(cap)
    if out != sorted(out):
        raise ValueError("capabilities must be sorted ascending and unique")
    return tuple(out)


# --------------------------------------------------------------------------
# strict JSON + primitive helpers (mirror contracts._load_strict_json)
# --------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    """Raised by the JSON object_pairs_hook on any duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def _load_strict_json(raw: object, label: str) -> object:
    if type(raw) is str:
        data = raw.encode("utf-8")
    elif type(raw) is bytes:
        data = raw
    else:
        raise TypeError(f"{label} must be str or bytes")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ValueError(f"{label} exceeds the maximum message size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc


def _require_exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return value


def _require_exact_keys(
    value: dict[str, object], allowed: frozenset[str], label: str
) -> None:
    if set(value.keys()) != allowed:
        raise ValueError(f"{label} must have exactly the required fields")


def _require_exact_bool(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")


def _require_exact_int(value: object, label: str) -> None:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")


def _require_request_id(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > _MAX_REQUEST_ID_LEN:
        raise ValueError(f"{label} must be a non-empty string (<=128 chars)")


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


def _require_optional_text(value: object, label: str, max_len: int) -> None:
    if value is None:
        return
    _require_bounded_text(value, label, max_len)


def _require_optional_identifier(value: object, label: str) -> None:
    if value is None:
        return
    _require_identifier(value, label)


def _decode_dt(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{label} must be an ISO timestamp string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid timestamp: {value!r}") from exc


# --------------------------------------------------------------------------
# ChangeSet / manifest deserializers (self-contained; mirror the repository)
# --------------------------------------------------------------------------


def _decode_owned(data: object) -> OwnedNodeRef:
    value = _require_exact_dict(data, "OwnedNodeRef")
    _require_exact_keys(value, _OWNED_FIELDS, "OwnedNodeRef")
    return OwnedNodeRef(
        node_id=value["node_id"],
        path=value["path"],
        node_type=value["node_type"],
        parent_path=value["parent_path"],
        capability=value["capability"],
        role=value["role"],
    )


def _decode_noderef(data: object) -> NodeRef:
    value = _require_exact_dict(data, "NodeRef")
    _require_exact_keys(value, _NODEREF_FIELDS, "NodeRef")
    return NodeRef(
        node_id=value["node_id"],
        path=value["path"],
        expected_type=value["expected_type"],
        expected_workspace_id=value["expected_workspace_id"],
    )


def _decode_wiref(data: object) -> WireRef:
    value = _require_exact_dict(data, "WireRef")
    _require_exact_keys(value, _WIREREF_FIELDS, "WireRef")
    return WireRef(
        source=_decode_noderef(value["source"]),
        source_output_index=value["source_output_index"],
    )


def _decode_operation(data: object) -> object:
    value = _require_exact_dict(data, "typed operation")
    kind = value.get("kind")
    if kind == "node.create":
        _require_exact_keys(value, _CREATE_FIELDS, "CreateNode")
        return CreateNode(
            op_id=value["op_id"],
            parent=_decode_noderef(value["parent"]),
            node_id=value["node_id"],
            node_type=value["node_type"],
            node_name=value["node_name"],
            workspace_id=value["workspace_id"],
            capability=value["capability"],
            role=value["role"],
        )
    if kind == "parm.set":
        _require_exact_keys(value, _SETPARM_FIELDS, "SetParm")
        return SetParm(
            op_id=value["op_id"],
            target=_decode_noderef(value["target"]),
            parm_name=value["parm_name"],
            value=value["value"],
            expected_old_value=value["expected_old_value"],
        )
    if kind == "wire.connect":
        _require_exact_keys(value, _CONNECT_FIELDS, "ConnectInput")
        old_source = value["expected_old_source"]
        return ConnectInput(
            op_id=value["op_id"],
            target=_decode_noderef(value["target"]),
            input_index=value["input_index"],
            source=_decode_noderef(value["source"]),
            source_output_index=value["source_output_index"],
            expected_old_source=(
                None if old_source is None else _decode_wiref(old_source)
            ),
        )
    raise ValueError(f"unsupported operation kind tag: {kind!r}")


def _decode_condition(data: object) -> object:
    value = _require_exact_dict(data, "condition")
    kind = value.get("kind")
    if kind == "scene.binding_equals":
        _require_exact_keys(
            value,
            frozenset({"kind", "instance_id", "scene_epoch"}),
            "SceneBindingEquals",
        )
        return SceneBindingEquals(
            instance_id=value["instance_id"], scene_epoch=value["scene_epoch"]
        )
    if kind == "workspace.revision_equals":
        _require_exact_keys(
            value,
            frozenset({"kind", "workspace_id", "revision"}),
            "WorkspaceRevisionEquals",
        )
        return WorkspaceRevisionEquals(
            workspace_id=value["workspace_id"], revision=value["revision"]
        )
    if kind == "node.identity_equals":
        _require_exact_keys(value, frozenset({"kind", "node"}), "NodeIdentityEquals")
        return NodeIdentityEquals(node=_decode_noderef(value["node"]))
    if kind == "parm.value_equals":
        _require_exact_keys(
            value,
            frozenset({"kind", "target", "parm_name", "value"}),
            "ParmValueEquals",
        )
        return ParmValueEquals(
            target=_decode_noderef(value["target"]),
            parm_name=value["parm_name"],
            value=value["value"],
        )
    if kind == "wire.input_equals":
        _require_exact_keys(
            value,
            frozenset({"kind", "target", "input_index", "source"}),
            "WireInputEquals",
        )
        source = value["source"]
        return WireInputEquals(
            target=_decode_noderef(value["target"]),
            input_index=value["input_index"],
            source=None if source is None else _decode_wiref(source),
        )
    if kind == "node.absent":
        _require_exact_keys(value, frozenset({"kind", "path", "node_id"}), "NodeAbsent")
        return NodeAbsent(path=value["path"], node_id=value["node_id"])
    raise ValueError(f"unsupported condition kind tag: {kind!r}")


def _decode_risk(data: object) -> RiskSummary:
    value = _require_exact_dict(data, "RiskSummary")
    _require_exact_keys(value, _RISK_FIELDS, "RiskSummary")
    return RiskSummary(
        touches_external_nodes=value["touches_external_nodes"],
        changes_wiring=value["changes_wiring"],
        requires_backup=value["requires_backup"],
        operation_count=value["operation_count"],
        effect_names=value["effect_names"],
        affected_paths=value["affected_paths"],
    )


def _decode_parm_snapshot(data: object) -> ParmSnapshot:
    value = _require_exact_dict(data, "ParmSnapshot")
    _require_exact_keys(value, _PARM_SNAPSHOT_FIELDS, "ParmSnapshot")
    return ParmSnapshot(
        target=_decode_noderef(value["target"]), parm_name=value["parm_name"]
    )


def _decode_wire_snapshot(data: object) -> WireSnapshot:
    value = _require_exact_dict(data, "WireSnapshot")
    _require_exact_keys(value, _WIRE_SNAPSHOT_FIELDS, "WireSnapshot")
    return WireSnapshot(
        target=_decode_noderef(value["target"]), input_index=value["input_index"]
    )


def _decode_checkpoint(data: object) -> CheckpointPlan:
    value = _require_exact_dict(data, "CheckpointPlan")
    _require_exact_keys(value, _CHECKPOINT_FIELDS, "CheckpointPlan")
    return CheckpointPlan(
        nodes=[_decode_noderef(n) for n in value["nodes"]],
        parameters=[_decode_parm_snapshot(p) for p in value["parameters"]],
        wires=[_decode_wire_snapshot(w) for w in value["wires"]],
    )


def _decode_condition_result(data: object) -> ConditionResult:
    value = _require_exact_dict(data, "ConditionResult")
    _require_exact_keys(value, _CONDITION_RESULT_FIELDS, "ConditionResult")
    return ConditionResult(
        kind=value["kind"], passed=value["passed"], detail=value["detail"]
    )


def _decode_manifest(data: object) -> WorkspaceManifest:
    value = _require_exact_dict(data, "WorkspaceManifest")
    _require_exact_keys(value, _MANIFEST_FIELDS, "WorkspaceManifest")
    return WorkspaceManifest(
        schema_version=value["schema_version"],
        workspace_id=value["workspace_id"],
        session_id=value["session_id"],
        instance_id=value["instance_id"],
        scene_epoch=value["scene_epoch"],
        revision=value["revision"],
        roots=[_decode_owned(r) for r in value["roots"]],
        nodes=[_decode_owned(n) for n in value["nodes"]],
        created_by_run=value["created_by_run"],
        updated_at=_decode_dt(value["updated_at"], "WorkspaceManifest.updated_at"),
    )


def _decode_changeset(data: object) -> ChangeSet:
    value = _require_exact_dict(data, "ChangeSet")
    _require_exact_keys(value, _CHANGESET_FIELDS, "ChangeSet")
    return ChangeSet(
        schema_version=value["schema_version"],
        change_id=value["change_id"],
        session_id=value["session_id"],
        run_id=value["run_id"],
        scene_binding=SceneBinding.from_dict(
            _require_exact_dict(value["scene_binding"], "ChangeSet.scene_binding")
        ),
        workspace_id=value["workspace_id"],
        base_revision=value["base_revision"],
        required_permission=PermissionMode(value["required_permission"]),
        scoped_node_ids=value["scoped_node_ids"],
        operations=[_decode_operation(op) for op in value["operations"]],
        affected_nodes=[_decode_noderef(n) for n in value["affected_nodes"]],
        read_dependencies=[_decode_noderef(n) for n in value["read_dependencies"]],
        preconditions=[_decode_condition(c) for c in value["preconditions"]],
        expected_postconditions=[
            _decode_condition(c) for c in value["expected_postconditions"]
        ],
        risk_summary=_decode_risk(value["risk_summary"]),
        checkpoint_plan=_decode_checkpoint(value["checkpoint_plan"]),
        created_at=_decode_dt(value["created_at"], "ChangeSet.created_at"),
    )


# --------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightRequest:
    """A parsed, validated ``changeset.preflight`` request envelope.

    Carries the full canonical schema-v1 :class:`ChangeSet` and its exact
    SHA-256 digest, plus the matching :class:`WorkspaceManifest` when the
    ChangeSet owns a workspace (otherwise ``None``). Construction verifies the
    digest, manifest identity/binding consistency, and that the envelope scene
    epoch agrees with the ChangeSet binding epoch.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    changeset: ChangeSet
    changeset_digest: str
    workspace: WorkspaceManifest | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "PreflightRequest.request_id")
        _require_exact_int(self.deadline_ms, "PreflightRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("PreflightRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "PreflightRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("PreflightRequest.scene_epoch must be >= 1")
        if type(self.changeset) is not ChangeSet:
            raise TypeError("PreflightRequest.changeset must be an exact ChangeSet")
        _require_sha256(self.changeset_digest, "PreflightRequest.changeset_digest")
        if self.changeset_digest != self.changeset.digest:
            raise ValueError(
                "PreflightRequest.changeset_digest must equal the canonical ChangeSet digest"
            )
        if self.workspace is not None and type(self.workspace) is not WorkspaceManifest:
            raise TypeError(
                "PreflightRequest.workspace must be an exact WorkspaceManifest or None"
            )
        binding = self.changeset.scene_binding
        if self.scene_epoch != binding.scene_epoch:
            raise ValueError(
                "PreflightRequest.scene_epoch must match the ChangeSet binding epoch"
            )
        if self.workspace is None:
            if self.changeset.workspace_id is not None:
                raise ValueError(
                    "PreflightRequest.workspace must be present when the ChangeSet owns a workspace"
                )
        else:
            if self.changeset.workspace_id is None:
                raise ValueError(
                    "PreflightRequest.workspace must be null when the ChangeSet owns no workspace"
                )
            if self.workspace.workspace_id != self.changeset.workspace_id:
                raise ValueError(
                    "PreflightRequest workspace_id must match the ChangeSet workspace_id"
                )
            if self.workspace.session_id != self.changeset.session_id:
                raise ValueError(
                    "PreflightRequest workspace session must match the ChangeSet session"
                )
            if self.workspace.instance_id != binding.instance_id:
                raise ValueError(
                    "PreflightRequest workspace instance must match the ChangeSet binding instance"
                )
            if self.workspace.scene_epoch != binding.scene_epoch:
                raise ValueError(
                    "PreflightRequest workspace scene epoch must match the ChangeSet binding epoch"
                )

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        changeset: ChangeSet,
        workspace: WorkspaceManifest | None = None,
    ) -> PreflightRequest:
        """Build a request, computing the canonical ChangeSet digest."""
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            changeset=changeset,
            changeset_digest=changeset.digest,
            workspace=workspace,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "changeset": self.changeset.to_dict(),
                "changeset_digest": self.changeset_digest,
                "workspace": (
                    self.workspace.to_dict() if self.workspace is not None else None
                ),
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PreflightRequest:
        envelope = _require_exact_dict(data, "PreflightRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "PreflightRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("PreflightRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("PreflightRequest kind must be request")
        if envelope["operation"] != OPERATION:
            raise ValueError("PreflightRequest operation must be changeset.preflight")
        payload = _require_exact_dict(envelope["payload"], "PreflightRequest payload")
        _require_exact_keys(payload, _REQUEST_PAYLOAD_FIELDS, "PreflightRequest payload")
        changeset = _decode_changeset(payload["changeset"])
        workspace = (
            None if payload["workspace"] is None else _decode_manifest(payload["workspace"])
        )
        return cls(
            request_id=envelope["request_id"],
            deadline_ms=envelope["deadline_ms"],
            scene_epoch=envelope["scene_epoch"],
            changeset=changeset,
            changeset_digest=payload["changeset_digest"],
            workspace=workspace,
        )


@dataclass(frozen=True, slots=True)
class PreflightNodeFact:
    """One deterministic, read-only fact about a referenced node."""

    requested: NodeRef
    exists: bool
    actual_path: str | None
    actual_type: str | None
    parent_path: str | None
    workspace_id: str | None
    node_id: str | None
    capability: str | None
    role: str | None
    is_locked: bool

    def __post_init__(self) -> None:
        if type(self.requested) is not NodeRef:
            raise TypeError("PreflightNodeFact.requested must be an exact NodeRef")
        _require_exact_bool(self.exists, "PreflightNodeFact.exists")
        _require_optional_text(self.actual_path, "PreflightNodeFact.actual_path", _MAX_NODE_PATH_LEN)
        _require_optional_text(self.actual_type, "PreflightNodeFact.actual_type", _MAX_NODE_TYPE_LEN)
        _require_optional_text(self.parent_path, "PreflightNodeFact.parent_path", _MAX_NODE_PATH_LEN)
        _require_optional_identifier(self.workspace_id, "PreflightNodeFact.workspace_id")
        _require_optional_identifier(self.node_id, "PreflightNodeFact.node_id")
        _require_optional_identifier(self.capability, "PreflightNodeFact.capability")
        _require_optional_identifier(self.role, "PreflightNodeFact.role")
        _require_exact_bool(self.is_locked, "PreflightNodeFact.is_locked")

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested.to_dict(),
            "exists": self.exists,
            "actual_path": self.actual_path,
            "actual_type": self.actual_type,
            "parent_path": self.parent_path,
            "workspace_id": self.workspace_id,
            "node_id": self.node_id,
            "capability": self.capability,
            "role": self.role,
            "is_locked": self.is_locked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PreflightNodeFact:
        value = _require_exact_dict(data, "PreflightNodeFact")
        _require_exact_keys(value, _NODE_FACT_FIELDS, "PreflightNodeFact")
        return cls(
            requested=_decode_noderef(value["requested"]),
            exists=value["exists"],
            actual_path=value["actual_path"],
            actual_type=value["actual_type"],
            parent_path=value["parent_path"],
            workspace_id=value["workspace_id"],
            node_id=value["node_id"],
            capability=value["capability"],
            role=value["role"],
            is_locked=value["is_locked"],
        )


@dataclass(frozen=True, slots=True)
class PreflightParmFact:
    """One read-only fact about a referenced parameter: existence + bounded value."""

    target: NodeRef
    parm_name: str
    exists: bool
    value: object | None

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("PreflightParmFact.target must be an exact NodeRef")
        _require_identifier(self.parm_name, "PreflightParmFact.parm_name")
        _require_exact_bool(self.exists, "PreflightParmFact.exists")
        if self.value is not None:
            object.__setattr__(
                self,
                "value",
                _validate_parm_value(self.value, "PreflightParmFact.value"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.to_dict(),
            "parm_name": self.parm_name,
            "exists": self.exists,
            "value": _parm_value_json(self.value) if self.value is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PreflightParmFact:
        value = _require_exact_dict(data, "PreflightParmFact")
        _require_exact_keys(value, _PARM_FACT_FIELDS, "PreflightParmFact")
        return cls(
            target=_decode_noderef(value["target"]),
            parm_name=value["parm_name"],
            exists=value["exists"],
            value=value["value"],
        )


@dataclass(frozen=True, slots=True)
class PreflightWireFact:
    """One read-only fact about a referenced input: the actual typed source or null."""

    target: NodeRef
    input_index: int
    source: WireRef | None

    def __post_init__(self) -> None:
        if type(self.target) is not NodeRef:
            raise TypeError("PreflightWireFact.target must be an exact NodeRef")
        _require_exact_int(self.input_index, "PreflightWireFact.input_index")
        if self.input_index < 0:
            raise ValueError("PreflightWireFact.input_index must be non-negative")
        if self.source is not None and type(self.source) is not WireRef:
            raise TypeError("PreflightWireFact.source must be an exact WireRef or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "target": self.target.to_dict(),
            "input_index": self.input_index,
            "source": self.source.to_dict() if self.source is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PreflightWireFact:
        value = _require_exact_dict(data, "PreflightWireFact")
        _require_exact_keys(value, _WIRE_FACT_FIELDS, "PreflightWireFact")
        source = value["source"]
        return cls(
            target=_decode_noderef(value["target"]),
            input_index=value["input_index"],
            source=None if source is None else _decode_wiref(source),
        )


def _freeze_facts(value: object, allowed: type, label: str) -> tuple[object, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise TypeError(f"PreflightResult.{label} must be a sequence")
    items = tuple(value)
    for item in items:
        if type(item) is not allowed:
            raise TypeError(
                f"PreflightResult.{label} entries must be exact {allowed.__name__}"
            )
    return items


def _require_sha256_or_none(value: object) -> None:
    if value is None:
        return
    _require_sha256(value, "PreflightResult.workspace_revision")


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """The bounded, typed, read-only result of a ``changeset.preflight``.

    ``scene_may_have_changed`` is always ``False``: preflight performs no write.
    Facts are deterministically ordered and deep immutable; an oversized result
    is rejected so a runaway fact set can never be returned to a caller.
    """

    binding: SceneBinding
    workspace_id: str | None
    workspace_revision: str | None
    node_facts: tuple[PreflightNodeFact, ...]
    parm_facts: tuple[PreflightParmFact, ...]
    wire_facts: tuple[PreflightWireFact, ...]
    condition_results: tuple[ConditionResult, ...]
    all_preconditions_hold: bool
    scene_may_have_changed: bool

    def __post_init__(self) -> None:
        if type(self.binding) is not SceneBinding:
            raise TypeError("PreflightResult.binding must be an exact SceneBinding")
        _require_optional_identifier(self.workspace_id, "PreflightResult.workspace_id")
        _require_sha256_or_none(self.workspace_revision)
        object.__setattr__(
            self, "node_facts", _freeze_facts(self.node_facts, PreflightNodeFact, "node_facts")
        )
        object.__setattr__(
            self, "parm_facts", _freeze_facts(self.parm_facts, PreflightParmFact, "parm_facts")
        )
        object.__setattr__(
            self, "wire_facts", _freeze_facts(self.wire_facts, PreflightWireFact, "wire_facts")
        )
        object.__setattr__(
            self,
            "condition_results",
            _freeze_facts(self.condition_results, ConditionResult, "condition_results"),
        )
        _require_exact_bool(self.all_preconditions_hold, "PreflightResult.all_preconditions_hold")
        _require_exact_bool(self.scene_may_have_changed, "PreflightResult.scene_may_have_changed")
        if self.scene_may_have_changed:
            raise ValueError("PreflightResult.scene_may_have_changed must be False")
        if len(canonical_json_dumps(self.to_dict()).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("PreflightResult exceeds the maximum result size")

    def to_dict(self) -> dict[str, object]:
        return {
            "binding": self.binding.to_dict(),
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "node_facts": [f.to_dict() for f in self.node_facts],
            "parm_facts": [f.to_dict() for f in self.parm_facts],
            "wire_facts": [f.to_dict() for f in self.wire_facts],
            "condition_results": [r.to_dict() for r in self.condition_results],
            "all_preconditions_hold": self.all_preconditions_hold,
            "scene_may_have_changed": self.scene_may_have_changed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PreflightResult:
        value = _require_exact_dict(data, "PreflightResult")
        _require_exact_keys(value, _RESULT_FIELDS, "PreflightResult")
        return cls(
            binding=SceneBinding.from_dict(value["binding"]),
            workspace_id=value["workspace_id"],
            workspace_revision=value["workspace_revision"],
            node_facts=[PreflightNodeFact.from_dict(f) for f in value["node_facts"]],
            parm_facts=[PreflightParmFact.from_dict(f) for f in value["parm_facts"]],
            wire_facts=[PreflightWireFact.from_dict(f) for f in value["wire_facts"]],
            condition_results=[
                _decode_condition_result(r) for r in value["condition_results"]
            ],
            all_preconditions_hold=value["all_preconditions_hold"],
            scene_may_have_changed=value["scene_may_have_changed"],
        )


@dataclass(frozen=True, slots=True)
class PreflightResponse:
    """A parsed, validated ``changeset.preflight`` response envelope."""

    request_id: str
    result: PreflightResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "PreflightResponse.request_id")
        if self.result is not None and type(self.result) is not PreflightResult:
            raise TypeError("PreflightResponse.result must be an exact PreflightResult or None")
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("PreflightResponse.error must be an exact BridgeError or None")
        if (self.result is None) == (self.error is None):
            raise ValueError("PreflightResponse must carry exactly one of result or error")

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
    def from_dict(cls, data: Mapping[str, object]) -> PreflightResponse:
        envelope = _require_exact_dict(data, "PreflightResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("PreflightResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("PreflightResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("PreflightResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("PreflightResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "PreflightResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError("PreflightResponse ok=true requires result and no error")
            return cls(
                request_id=envelope["request_id"],
                result=PreflightResult.from_dict(result),
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError("PreflightResponse ok=false requires error and no result")
        return cls(
            request_id=envelope["request_id"],
            result=None,
            error=BridgeError.from_dict(error),
        )


# --------------------------------------------------------------------------
# JSON text entrypoints
# --------------------------------------------------------------------------


def parse_preflight_request(raw: str | bytes) -> PreflightRequest:
    """Parse a ``changeset.preflight`` request from strict JSON text."""
    return PreflightRequest.from_dict(_load_strict_json(raw, "Preflight request"))


def parse_preflight_response(raw: str | bytes) -> PreflightResponse:
    """Parse a ``changeset.preflight`` response from strict JSON text."""
    return PreflightResponse.from_dict(_load_strict_json(raw, "Preflight response"))
