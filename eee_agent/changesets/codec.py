"""Shared strict ChangeSet/Manifest/Receipt DTO codec (pure contract layer).

Both :mod:`eee_agent.changesets.repository` (persistence) and
:mod:`eee_agent.houdini_bridge.changesets` (Bridge wire) previously carried
near-identical, duplicated strict decoders that turned a JSON-derived
``object`` into the frozen Task 16-A DTOs. They diverged only in spelling
(``_require_dict`` vs ``_require_exact_dict``) and both fed untyped
``object`` values straight into typed dataclass fields, producing a large
mypy fan-out.

This module owns the single shared "decode an already-obtained JSON-derived
object into a strict DTO" path. It is deliberately **not** a storage loader
(canonical JSON / digest verification stay in ``repository.py``) and **not**
a Bridge wire loader (message size / UTF-8 / duplicate-key rejection stay in
``houdini_bridge/changesets.py``). It imports only the DTO contracts and
``SceneBinding``; it never imports a repository, runtime, bridge envelope, or
transport module, so it cannot form an import cycle.

Strictness contract (unchanged from the two originals, now unified):

* exact ``type(value) is ...`` checks everywhere (``bool`` never satisfies an
  ``int`` field, a ``dict`` subclass never satisfies an exact dict);
* exact field sets per DTO (unknown / missing fields rejected);
* every typed value is produced by a validator that returns the narrowed
  type, so no unvalidated ``object`` reaches a dataclass field;
* collection fields are built as ``tuple`` to match the frozen DTO
  annotations;
* no ``typing.Any`` and no ``# type: ignore``; ``cast`` is used only where a
  value has already passed an exact-type check in the same control flow and
  mypy still cannot express the resulting type.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
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
    ReceiptStatus,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
)
from eee_agent.houdini_bridge.contracts import SceneBinding

# --------------------------------------------------------------------------
# canonical field sets (merged from repository.py and houdini_bridge/changesets.py;
# the two originals were identical for every shared DTO)
# --------------------------------------------------------------------------

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
_PARM_SNAPSHOT_FIELDS = frozenset({"target", "parm_name"})
_WIRE_SNAPSHOT_FIELDS = frozenset({"target", "input_index"})
_CHECKPOINT_FIELDS = frozenset({"nodes", "parameters", "wires"})
_CONDITION_RESULT_FIELDS = frozenset({"kind", "passed", "detail"})
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
_APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "approval_id",
        "change_id",
        "changeset_digest",
        "decision",
        "decided_by",
        "requested_at",
        "decided_at",
        "expires_at",
        "approved_instance_id",
        "approved_scene_epoch",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "change_id",
        "status",
        "instance_id",
        "scene_epoch",
        "before_revision",
        "after_revision",
        "applied_op_ids",
        "postcondition_results",
        "rollback_results",
        "scene_may_have_changed",
        "completed_at",
    }
)

# Per-condition field sets, inlined in the originals; centralized here.
_SCENE_BINDING_EQUALS_FIELDS = frozenset({"kind", "instance_id", "scene_epoch"})
_WORKSPACE_REVISION_EQUALS_FIELDS = frozenset(
    {"kind", "workspace_id", "revision"}
)
_NODE_IDENTITY_EQUALS_FIELDS = frozenset({"kind", "node"})
_PARM_VALUE_EQUALS_FIELDS = frozenset({"kind", "target", "parm_name", "value"})
_WIRE_INPUT_EQUALS_FIELDS = frozenset(
    {"kind", "target", "input_index", "source"}
)
_NODE_ABSENT_FIELDS = frozenset({"kind", "path", "node_id"})


# --------------------------------------------------------------------------
# typed primitives: each validator returns the narrowed type after an exact
# runtime check, so callers never hand an unvalidated object to a DTO field.
# --------------------------------------------------------------------------


def _require_exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return value


def _require_exact_keys(
    value: dict[str, object], allowed: frozenset[str], label: str
) -> None:
    if set(value.keys()) != allowed:
        raise ValueError(f"{label} must have exactly the required fields")


def _require_str(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    return value


def _require_optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError(f"{label} must be a string or None")
    return value


def _require_int(value: object, label: str) -> int:
    # bool is an int subclass: reject it explicitly so a JSON true/false can
    # never satisfy an integer field.
    if type(value) is bool:
        raise TypeError(f"{label} must be an integer, not a bool")
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


def _require_optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, label)


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")
    return value


def _require_optional_bool(value: object, label: str) -> bool | None:
    if value is None:
        return None
    return _require_bool(value, label)


def _require_exact_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise TypeError(f"{label} must be an exact list")
    return cast(list[object], value)


def _decode_str_tuple(value: object, label: str) -> tuple[str, ...]:
    # Build a tuple[str, ...] from a JSON array of exact strings. The frozen
    # DTO re-validates (sorted/deduped where applicable), but the codec hands
    # it the exact tuple type its field annotation promises.
    items = _require_exact_list(value, label)
    return tuple(_require_str(item, f"{label}[]") for item in items)


def _decode_datetime(value: object, label: str) -> datetime:
    text = _require_str(value, label)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid timestamp: {text!r}") from exc


# --------------------------------------------------------------------------
# DTO decoders — every return type is the exact frozen DTO.
# --------------------------------------------------------------------------


def decode_owned_node_ref(data: object) -> OwnedNodeRef:
    value = _require_exact_dict(data, "OwnedNodeRef")
    _require_exact_keys(value, _OWNED_FIELDS, "OwnedNodeRef")
    return OwnedNodeRef(
        node_id=_require_str(value["node_id"], "OwnedNodeRef.node_id"),
        path=_require_str(value["path"], "OwnedNodeRef.path"),
        node_type=_require_str(value["node_type"], "OwnedNodeRef.node_type"),
        parent_path=_require_str(value["parent_path"], "OwnedNodeRef.parent_path"),
        capability=_require_str(value["capability"], "OwnedNodeRef.capability"),
        role=_require_str(value["role"], "OwnedNodeRef.role"),
    )


def decode_node_ref(data: object) -> NodeRef:
    value = _require_exact_dict(data, "NodeRef")
    _require_exact_keys(value, _NODEREF_FIELDS, "NodeRef")
    return NodeRef(
        node_id=_require_optional_str(value["node_id"], "NodeRef.node_id"),
        path=_require_str(value["path"], "NodeRef.path"),
        expected_type=_require_str(value["expected_type"], "NodeRef.expected_type"),
        expected_workspace_id=_require_optional_str(
            value["expected_workspace_id"], "NodeRef.expected_workspace_id"
        ),
    )


def decode_wire_ref(data: object) -> WireRef:
    value = _require_exact_dict(data, "WireRef")
    _require_exact_keys(value, _WIREREF_FIELDS, "WireRef")
    return WireRef(
        source=decode_node_ref(value["source"]),
        source_output_index=_require_int(
            value["source_output_index"], "WireRef.source_output_index"
        ),
    )


def decode_operation(data: object) -> CreateNode | SetParm | ConnectInput:
    value = _require_exact_dict(data, "typed operation")
    kind = value.get("kind")
    if kind == "node.create":
        _require_exact_keys(value, _CREATE_FIELDS, "CreateNode")
        return CreateNode(
            op_id=_require_str(value["op_id"], "CreateNode.op_id"),
            parent=decode_node_ref(value["parent"]),
            node_id=_require_str(value["node_id"], "CreateNode.node_id"),
            node_type=_require_str(value["node_type"], "CreateNode.node_type"),
            node_name=_require_str(value["node_name"], "CreateNode.node_name"),
            workspace_id=_require_str(value["workspace_id"], "CreateNode.workspace_id"),
            capability=_require_str(value["capability"], "CreateNode.capability"),
            role=_require_str(value["role"], "CreateNode.role"),
        )
    if kind == "parm.set":
        _require_exact_keys(value, _SETPARM_FIELDS, "SetParm")
        return SetParm(
            op_id=_require_str(value["op_id"], "SetParm.op_id"),
            target=decode_node_ref(value["target"]),
            parm_name=_require_str(value["parm_name"], "SetParm.parm_name"),
            value=value["value"],
            expected_old_value=value["expected_old_value"],
        )
    if kind == "wire.connect":
        _require_exact_keys(value, _CONNECT_FIELDS, "ConnectInput")
        old_source = value["expected_old_source"]
        return ConnectInput(
            op_id=_require_str(value["op_id"], "ConnectInput.op_id"),
            target=decode_node_ref(value["target"]),
            input_index=_require_int(value["input_index"], "ConnectInput.input_index"),
            source=decode_node_ref(value["source"]),
            source_output_index=_require_int(
                value["source_output_index"], "ConnectInput.source_output_index"
            ),
            expected_old_source=(
                None if old_source is None else decode_wire_ref(old_source)
            ),
        )
    raise ValueError(f"unsupported operation kind tag: {kind!r}")


def decode_condition(
    data: object,
) -> (
    SceneBindingEquals
    | WorkspaceRevisionEquals
    | NodeIdentityEquals
    | ParmValueEquals
    | WireInputEquals
    | NodeAbsent
):
    value = _require_exact_dict(data, "condition")
    kind = value.get("kind")
    if kind == "scene.binding_equals":
        _require_exact_keys(
            value, _SCENE_BINDING_EQUALS_FIELDS, "SceneBindingEquals"
        )
        return SceneBindingEquals(
            instance_id=_require_str(
                value["instance_id"], "SceneBindingEquals.instance_id"
            ),
            scene_epoch=_require_int(
                value["scene_epoch"], "SceneBindingEquals.scene_epoch"
            ),
        )
    if kind == "workspace.revision_equals":
        _require_exact_keys(
            value, _WORKSPACE_REVISION_EQUALS_FIELDS, "WorkspaceRevisionEquals"
        )
        return WorkspaceRevisionEquals(
            workspace_id=_require_str(
                value["workspace_id"], "WorkspaceRevisionEquals.workspace_id"
            ),
            revision=_require_str(
                value["revision"], "WorkspaceRevisionEquals.revision"
            ),
        )
    if kind == "node.identity_equals":
        _require_exact_keys(
            value, _NODE_IDENTITY_EQUALS_FIELDS, "NodeIdentityEquals"
        )
        return NodeIdentityEquals(node=decode_node_ref(value["node"]))
    if kind == "parm.value_equals":
        _require_exact_keys(
            value, _PARM_VALUE_EQUALS_FIELDS, "ParmValueEquals"
        )
        return ParmValueEquals(
            target=decode_node_ref(value["target"]),
            parm_name=_require_str(value["parm_name"], "ParmValueEquals.parm_name"),
            value=value["value"],
        )
    if kind == "wire.input_equals":
        _require_exact_keys(
            value, _WIRE_INPUT_EQUALS_FIELDS, "WireInputEquals"
        )
        source = value["source"]
        return WireInputEquals(
            target=decode_node_ref(value["target"]),
            input_index=_require_int(
                value["input_index"], "WireInputEquals.input_index"
            ),
            source=None if source is None else decode_wire_ref(source),
        )
    if kind == "node.absent":
        _require_exact_keys(value, _NODE_ABSENT_FIELDS, "NodeAbsent")
        return NodeAbsent(
            path=_require_str(value["path"], "NodeAbsent.path"),
            node_id=_require_str(value["node_id"], "NodeAbsent.node_id"),
        )
    raise ValueError(f"unsupported condition kind tag: {kind!r}")


def decode_postcondition(
    data: object,
) -> NodeIdentityEquals | ParmValueEquals | WireInputEquals:
    """Decode a condition and narrow it to a Postcondition.

    The ChangeSet wire schema allows the six condition kinds in both the
    ``preconditions`` and ``expected_postconditions`` arrays, but the frozen
    :class:`ChangeSet` only accepts the three postcondition types in
    ``expected_postconditions`` (enforced by its ``__post_init__``, which raises
    ``TypeError``). Decoding via :func:`decode_condition` then rejecting a
    non-postcondition kind here surfaces that contract violation at the decode
    boundary with the same ``TypeError`` the DTO raises, rather than handing
    the DTO an out-of-contract object.

    Exact ``type(cond) is ...`` branches (no ``isinstance``) keep the contract
    boundary on the exact DTO types; each branch returns the already-narrowed
    object so no ``cast`` is needed.
    """
    condition = decode_condition(data)

    if type(condition) is NodeIdentityEquals:
        return condition
    if type(condition) is ParmValueEquals:
        return condition
    if type(condition) is WireInputEquals:
        return condition

    raise TypeError(
        "ChangeSet.expected_postconditions entries must be "
        "NodeIdentityEquals, ParmValueEquals, or WireInputEquals"
    )


def decode_risk_summary(data: object) -> RiskSummary:
    value = _require_exact_dict(data, "RiskSummary")
    _require_exact_keys(value, _RISK_FIELDS, "RiskSummary")
    return RiskSummary(
        touches_external_nodes=_require_bool(
            value["touches_external_nodes"], "RiskSummary.touches_external_nodes"
        ),
        changes_wiring=_require_bool(
            value["changes_wiring"], "RiskSummary.changes_wiring"
        ),
        requires_backup=_require_bool(
            value["requires_backup"], "RiskSummary.requires_backup"
        ),
        operation_count=_require_int(
            value["operation_count"], "RiskSummary.operation_count"
        ),
        effect_names=_decode_str_tuple(
            value["effect_names"], "RiskSummary.effect_names"
        ),
        affected_paths=_decode_str_tuple(
            value["affected_paths"], "RiskSummary.affected_paths"
        ),
    )


def decode_parm_snapshot(data: object) -> ParmSnapshot:
    value = _require_exact_dict(data, "ParmSnapshot")
    _require_exact_keys(value, _PARM_SNAPSHOT_FIELDS, "ParmSnapshot")
    return ParmSnapshot(
        target=decode_node_ref(value["target"]),
        parm_name=_require_str(value["parm_name"], "ParmSnapshot.parm_name"),
    )


def decode_wire_snapshot(data: object) -> WireSnapshot:
    value = _require_exact_dict(data, "WireSnapshot")
    _require_exact_keys(value, _WIRE_SNAPSHOT_FIELDS, "WireSnapshot")
    return WireSnapshot(
        target=decode_node_ref(value["target"]),
        input_index=_require_int(value["input_index"], "WireSnapshot.input_index"),
    )


def decode_checkpoint_plan(data: object) -> CheckpointPlan:
    value = _require_exact_dict(data, "CheckpointPlan")
    _require_exact_keys(value, _CHECKPOINT_FIELDS, "CheckpointPlan")
    return CheckpointPlan(
        nodes=tuple(
            decode_node_ref(n)
            for n in _require_exact_list(value["nodes"], "CheckpointPlan.nodes")
        ),
        parameters=tuple(
            decode_parm_snapshot(p)
            for p in _require_exact_list(
                value["parameters"], "CheckpointPlan.parameters"
            )
        ),
        wires=tuple(
            decode_wire_snapshot(w)
            for w in _require_exact_list(value["wires"], "CheckpointPlan.wires")
        ),
    )


def decode_condition_result(data: object) -> ConditionResult:
    value = _require_exact_dict(data, "ConditionResult")
    _require_exact_keys(value, _CONDITION_RESULT_FIELDS, "ConditionResult")
    return ConditionResult(
        kind=_require_str(value["kind"], "ConditionResult.kind"),
        passed=_require_bool(value["passed"], "ConditionResult.passed"),
        detail=_require_optional_str(value["detail"], "ConditionResult.detail"),
    )


def decode_workspace_manifest(data: object) -> WorkspaceManifest:
    value = _require_exact_dict(data, "WorkspaceManifest")
    _require_exact_keys(value, _MANIFEST_FIELDS, "WorkspaceManifest")
    return WorkspaceManifest(
        schema_version=_require_int(
            value["schema_version"], "WorkspaceManifest.schema_version"
        ),
        workspace_id=_require_str(
            value["workspace_id"], "WorkspaceManifest.workspace_id"
        ),
        session_id=_require_str(value["session_id"], "WorkspaceManifest.session_id"),
        instance_id=_require_str(
            value["instance_id"], "WorkspaceManifest.instance_id"
        ),
        scene_epoch=_require_int(
            value["scene_epoch"], "WorkspaceManifest.scene_epoch"
        ),
        revision=_require_str(value["revision"], "WorkspaceManifest.revision"),
        roots=tuple(
            decode_owned_node_ref(r)
            for r in _require_exact_list(value["roots"], "WorkspaceManifest.roots")
        ),
        nodes=tuple(
            decode_owned_node_ref(n)
            for n in _require_exact_list(value["nodes"], "WorkspaceManifest.nodes")
        ),
        created_by_run=_require_str(
            value["created_by_run"], "WorkspaceManifest.created_by_run"
        ),
        updated_at=_decode_datetime(
            value["updated_at"], "WorkspaceManifest.updated_at"
        ),
    )


def decode_changeset(data: object) -> ChangeSet:
    value = _require_exact_dict(data, "ChangeSet")
    _require_exact_keys(value, _CHANGESET_FIELDS, "ChangeSet")
    return ChangeSet(
        schema_version=_require_int(
            value["schema_version"], "ChangeSet.schema_version"
        ),
        change_id=_require_str(value["change_id"], "ChangeSet.change_id"),
        session_id=_require_str(value["session_id"], "ChangeSet.session_id"),
        run_id=_require_str(value["run_id"], "ChangeSet.run_id"),
        scene_binding=SceneBinding.from_dict(
            _require_exact_dict(value["scene_binding"], "ChangeSet.scene_binding")
        ),
        workspace_id=_require_optional_str(
            value["workspace_id"], "ChangeSet.workspace_id"
        ),
        base_revision=_require_str(
            value["base_revision"], "ChangeSet.base_revision"
        ),
        required_permission=PermissionMode(
            _require_str(
                value["required_permission"], "ChangeSet.required_permission"
            )
        ),
        scoped_node_ids=_decode_str_tuple(
            value["scoped_node_ids"], "ChangeSet.scoped_node_ids"
        ),
        operations=tuple(
            decode_operation(op)
            for op in _require_exact_list(value["operations"], "ChangeSet.operations")
        ),
        affected_nodes=tuple(
            decode_node_ref(n)
            for n in _require_exact_list(
                value["affected_nodes"], "ChangeSet.affected_nodes"
            )
        ),
        read_dependencies=tuple(
            decode_node_ref(n)
            for n in _require_exact_list(
                value["read_dependencies"], "ChangeSet.read_dependencies"
            )
        ),
        preconditions=tuple(
            decode_condition(c)
            for c in _require_exact_list(
                value["preconditions"], "ChangeSet.preconditions"
            )
        ),
        expected_postconditions=tuple(
            decode_postcondition(c)
            for c in _require_exact_list(
                value["expected_postconditions"],
                "ChangeSet.expected_postconditions",
            )
        ),
        risk_summary=decode_risk_summary(value["risk_summary"]),
        checkpoint_plan=decode_checkpoint_plan(value["checkpoint_plan"]),
        created_at=_decode_datetime(value["created_at"], "ChangeSet.created_at"),
    )


def decode_approval_record(data: object) -> ApprovalRecord:
    value = _require_exact_dict(data, "ApprovalRecord")
    _require_exact_keys(value, _APPROVAL_FIELDS, "ApprovalRecord")
    decided_at = value["decided_at"]
    return ApprovalRecord(
        schema_version=_require_int(
            value["schema_version"], "ApprovalRecord.schema_version"
        ),
        approval_id=_require_str(
            value["approval_id"], "ApprovalRecord.approval_id"
        ),
        change_id=_require_str(value["change_id"], "ApprovalRecord.change_id"),
        changeset_digest=_require_str(
            value["changeset_digest"], "ApprovalRecord.changeset_digest"
        ),
        decision=ApprovalDecision(
            _require_str(value["decision"], "ApprovalRecord.decision")
        ),
        decided_by=_require_optional_str(
            value["decided_by"], "ApprovalRecord.decided_by"
        ),
        requested_at=_decode_datetime(
            value["requested_at"], "ApprovalRecord.requested_at"
        ),
        decided_at=(
            None
            if decided_at is None
            else _decode_datetime(decided_at, "ApprovalRecord.decided_at")
        ),
        expires_at=_decode_datetime(
            value["expires_at"], "ApprovalRecord.expires_at"
        ),
        approved_instance_id=_require_optional_str(
            value["approved_instance_id"], "ApprovalRecord.approved_instance_id"
        ),
        approved_scene_epoch=_require_optional_int(
            value["approved_scene_epoch"], "ApprovalRecord.approved_scene_epoch"
        ),
    )


def decode_change_receipt(data: object) -> ChangeReceipt:
    value = _require_exact_dict(data, "ChangeReceipt")
    _require_exact_keys(value, _RECEIPT_FIELDS, "ChangeReceipt")
    return ChangeReceipt(
        schema_version=_require_int(
            value["schema_version"], "ChangeReceipt.schema_version"
        ),
        change_id=_require_str(value["change_id"], "ChangeReceipt.change_id"),
        status=ReceiptStatus(_require_str(value["status"], "ChangeReceipt.status")),
        instance_id=_require_str(value["instance_id"], "ChangeReceipt.instance_id"),
        scene_epoch=_require_int(
            value["scene_epoch"], "ChangeReceipt.scene_epoch"
        ),
        before_revision=_require_str(
            value["before_revision"], "ChangeReceipt.before_revision"
        ),
        after_revision=_require_str(
            value["after_revision"], "ChangeReceipt.after_revision"
        ),
        applied_op_ids=_decode_str_tuple(
            value["applied_op_ids"], "ChangeReceipt.applied_op_ids"
        ),
        postcondition_results=tuple(
            decode_condition_result(r)
            for r in _require_exact_list(
                value["postcondition_results"],
                "ChangeReceipt.postcondition_results",
            )
        ),
        rollback_results=tuple(
            decode_condition_result(r)
            for r in _require_exact_list(
                value["rollback_results"], "ChangeReceipt.rollback_results"
            )
        ),
        scene_may_have_changed=_require_bool(
            value["scene_may_have_changed"],
            "ChangeReceipt.scene_may_have_changed",
        ),
        completed_at=_decode_datetime(
            value["completed_at"], "ChangeReceipt.completed_at"
        ),
    )
