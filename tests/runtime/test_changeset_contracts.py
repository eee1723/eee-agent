"""Task 16-A: immutable typed ChangeSet / WorkspaceManifest contract tests.

Pure-Python RED/GREEN tests for the frozen, JSON-canonical DTOs in
:mod:`eee_agent.changesets.contracts`. No ``hou``, ``rpyc``, database,
transport, or Houdini process is exercised here.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from eee_agent.changesets import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    Effect,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmSnapshot,
    ParmValueEquals,
    PermissionMode,
    PolicyDecision,
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
# shared constants + factories
# --------------------------------------------------------------------------

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
WS2 = f"ws_{'3' * 32}"
CHG = f"chg_{'4' * 32}"
APR = f"apr_{'5' * 32}"
REVISION = "a" * 64
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(seconds=30)


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = dict(
        instance_id="hou_instance_1",
        scene_epoch=1,
        hip_path=None,
        observed_revision="rev-1",
    )
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _owned(**overrides: object) -> OwnedNodeRef:
    values: dict[str, object] = dict(
        node_id="n_root",
        path="/obj/ws",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    values.update(overrides)
    return OwnedNodeRef(**values)  # type: ignore[arg-type]


def _noderef(**overrides: object) -> NodeRef:
    values: dict[str, object] = dict(
        node_id="n_child",
        path="/obj/ws/geo1",
        expected_type="geo",
        expected_workspace_id=WS,
    )
    values.update(overrides)
    return NodeRef(**values)  # type: ignore[arg-type]


def _wireref(**overrides: object) -> WireRef:
    values: dict[str, object] = dict(source=_noderef(), source_output_index=0)
    values.update(overrides)
    return WireRef(**values)  # type: ignore[arg-type]


def _create(**overrides: object) -> CreateNode:
    values: dict[str, object] = dict(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="subnet"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    values.update(overrides)
    return CreateNode(**values)  # type: ignore[arg-type]


def _setparm(**overrides: object) -> SetParm:
    values: dict[str, object] = dict(
        op_id="op_parm",
        target=_noderef(),
        parm_name="tx",
        value=0,
        expected_old_value=0,
    )
    values.update(overrides)
    return SetParm(**values)  # type: ignore[arg-type]


def _connect(**overrides: object) -> ConnectInput:
    values: dict[str, object] = dict(
        op_id="op_wire",
        target=_noderef(node_id="n_in", path="/obj/ws/in1", expected_type="merge"),
        input_index=0,
        source=_noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform"),
        source_output_index=0,
        expected_old_source=None,
    )
    values.update(overrides)
    return ConnectInput(**values)  # type: ignore[arg-type]


def _roots_nodes(count: int = 2) -> tuple[list[OwnedNodeRef], list[OwnedNodeRef]]:
    root = _owned()
    children = [
        _owned(
            node_id=f"n_child_{i}",
            path=f"/obj/ws/c{i}",
            parent_path="/obj/ws",
            role="member",
        )
        for i in range(max(count - 1, 0))
    ]
    nodes = [root, *children]
    return [root], nodes


def _manifest(**overrides: object) -> WorkspaceManifest:
    roots, nodes = _roots_nodes()
    values: dict[str, object] = dict(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=roots,
        nodes=nodes,
        created_by_run=RUN,
        updated_at=NOW,
    )
    values.update(overrides)
    return WorkspaceManifest.build(**values)  # type: ignore[arg-type]


def _risk(**overrides: object) -> RiskSummary:
    values: dict[str, object] = dict(
        touches_external_nodes=False,
        changes_wiring=False,
        requires_backup=False,
        operation_count=1,
        effect_names=("node.create",),
        affected_paths=("/obj/ws/geo_new",),
    )
    values.update(overrides)
    return RiskSummary(**values)  # type: ignore[arg-type]


def _checkpoint(**overrides: object) -> CheckpointPlan:
    values: dict[str, object] = dict(nodes=(), parameters=(), wires=())
    values.update(overrides)
    return CheckpointPlan(**values)  # type: ignore[arg-type]


def _changeset(**overrides: object) -> ChangeSet:
    values: dict[str, object] = dict(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(_create(),),
        affected_nodes=(_noderef(node_id="n_new", path="/obj/ws/geo_new"),),
        read_dependencies=(),
        preconditions=(),
        expected_postconditions=(),
        risk_summary=_risk(),
        checkpoint_plan=_checkpoint(),
        created_at=NOW,
    )
    values.update(overrides)
    return ChangeSet(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


def test_permission_mode_values() -> None:
    assert PermissionMode.OWNED_WORKSPACE == "OwnedWorkspace"
    assert PermissionMode.SCOPED_PATCH == "ScopedPatch"
    assert PermissionMode.PROJECT_CHANGE == "ProjectChange"


def test_effect_values_exact() -> None:
    assert Effect.NODE_CREATE == "node.create"
    assert Effect.PARM_SET == "parm.set"
    assert Effect.WIRE_CONNECT == "wire.connect"
    assert {e.value for e in Effect} == {
        "node.create",
        "parm.set",
        "wire.connect",
    }


def test_approval_decision_values() -> None:
    assert ApprovalDecision.PENDING == "Pending"
    assert ApprovalDecision.APPROVED == "Approved"
    assert ApprovalDecision.REJECTED == "Rejected"
    assert ApprovalDecision.CONSUMED == "Consumed"
    assert ApprovalDecision.EXPIRED == "Expired"


def test_receipt_status_values() -> None:
    assert ReceiptStatus.APPLIED == "Applied"
    assert ReceiptStatus.ALREADY_APPLIED == "AlreadyApplied"
    assert ReceiptStatus.ROLLED_BACK == "RolledBack"
    assert ReceiptStatus.PARTIAL == "Partial"
    assert ReceiptStatus.CRITICAL_RECOVERY == "CriticalRecovery"


# --------------------------------------------------------------------------
# OwnedNodeRef / NodeRef / WireRef strictness
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(node_id="", path="/obj", node_type="geo", parent_path="/obj", capability="c", role="r"),
        dict(node_id="n", path="obj/rel", node_type="geo", parent_path="/obj", capability="c", role="r"),
        dict(node_id="n", path="/obj\0bad", node_type="geo", parent_path="/obj", capability="c", role="r"),
        dict(node_id="n", path="/obj", node_type="", parent_path="/obj", capability="c", role="r"),
        dict(node_id="bad space", path="/obj", node_type="geo", parent_path="/obj", capability="c", role="r"),
        dict(node_id="n", path="/obj", node_type="geo", parent_path="/obj", capability="c", role="bad role"),
    ],
    ids=["empty-id", "rel-path", "nul-path", "empty-type", "bad-id", "bad-role"],
)
def test_owned_node_ref_rejects_invalid(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        OwnedNodeRef(**kwargs)  # type: ignore[arg-type]


def test_owned_node_ref_rejects_overlong_path() -> None:
    with pytest.raises((ValueError, TypeError)):
        _owned(path="/" + "a" * 5000)


def test_owned_node_ref_rejects_overlong_identifier() -> None:
    with pytest.raises((ValueError, TypeError)):
        _owned(node_id="n" * 5000)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(node_id="n", path="", expected_type="geo", expected_workspace_id=WS),
        dict(node_id="n", path="/obj", expected_type="", expected_workspace_id=WS),
        dict(node_id="n", path="/obj", expected_type="geo", expected_workspace_id="bad"),
    ],
    ids=["empty-path", "empty-type", "bad-workspace"],
)
def test_node_ref_rejects_invalid(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        NodeRef(**kwargs)  # type: ignore[arg-type]


def test_node_ref_external_allows_null_ids() -> None:
    ref = NodeRef(node_id=None, path="/obj/ext", expected_type="geo", expected_workspace_id=None)
    assert ref.node_id is None
    assert ref.expected_workspace_id is None


def test_wire_ref_rejects_negative_or_bool_index() -> None:
    with pytest.raises((TypeError, ValueError)):
        WireRef(source=_noderef(), source_output_index=-1)
    with pytest.raises((TypeError, ValueError)):
        WireRef(source=_noderef(), source_output_index=True)


def test_wire_ref_rejects_non_noderef_source() -> None:
    with pytest.raises((TypeError, ValueError)):
        WireRef(source="/obj", source_output_index=0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# WorkspaceManifest
# --------------------------------------------------------------------------


def test_manifest_build_computes_verifiable_revision() -> None:
    manifest = _manifest()
    assert manifest.schema_version == 1
    assert len(manifest.revision) == 64
    # constructor accepts an explicitly computed revision
    again = WorkspaceManifest(
        schema_version=1,
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        revision=manifest.revision,
        roots=manifest.roots,
        nodes=manifest.nodes,
        created_by_run=RUN,
        updated_at=NOW,
    )
    assert again.revision == manifest.revision


def test_manifest_rejects_wrong_revision() -> None:
    with pytest.raises(ValueError, match="revision"):
        WorkspaceManifest(
            schema_version=1,
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            revision="b" * 64,
            roots=[_owned()],
            nodes=[_owned()],
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_rejects_wrong_schema_version_value() -> None:
    manifest = _manifest()
    with pytest.raises((TypeError, ValueError), match="schema_version"):
        WorkspaceManifest(
            schema_version=2,
            workspace_id=manifest.workspace_id,
            session_id=manifest.session_id,
            instance_id=manifest.instance_id,
            scene_epoch=manifest.scene_epoch,
            revision=manifest.revision,
            roots=manifest.roots,
            nodes=manifest.nodes,
            created_by_run=manifest.created_by_run,
            updated_at=manifest.updated_at,
        )


def test_manifest_root_must_be_in_nodes() -> None:
    with pytest.raises((ValueError, TypeError), match="root"):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[_owned(node_id="n_root", path="/obj/ws")],
            nodes=[_owned(node_id="n_other", path="/obj/other", parent_path="/obj")],
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_root_facts_must_match_node() -> None:
    root = _owned(node_id="n_root", path="/obj/ws", capability="modeling", role="root")
    different_capability = _owned(
        node_id="n_root", path="/obj/ws", capability="rigging", role="root"
    )
    with pytest.raises((ValueError, TypeError), match="root"):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[root],
            nodes=[different_capability],
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_rejects_duplicate_node_id() -> None:
    with pytest.raises((ValueError, TypeError), match="duplicate"):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[_owned()],
            nodes=[
                _owned(node_id="n_root", path="/obj/ws"),
                _owned(node_id="n_root", path="/obj/ws2", parent_path="/obj"),
            ],
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_rejects_duplicate_path() -> None:
    with pytest.raises((ValueError, TypeError), match="duplicate"):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[_owned()],
            nodes=[
                _owned(node_id="n_root", path="/obj/ws"),
                _owned(node_id="n_other", path="/obj/ws", parent_path="/obj"),
            ],
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_rejects_empty_or_too_many_roots() -> None:
    roots, nodes = _roots_nodes()
    with pytest.raises((ValueError, TypeError)):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[],
            nodes=nodes,
            created_by_run=RUN,
            updated_at=NOW,
        )
    too_many_roots = []
    all_nodes = []
    for i in range(17):
        ref = _owned(node_id=f"n_r{i}", path=f"/obj/r{i}", parent_path="/obj")
        too_many_roots.append(ref)
        all_nodes.append(ref)
    with pytest.raises((ValueError, TypeError)):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=too_many_roots,
            nodes=all_nodes,
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_accepts_sixteen_roots_and_bound() -> None:
    roots = []
    for i in range(16):
        ref = _owned(node_id=f"n_r{i}", path=f"/obj/r{i}", parent_path="/obj")
        roots.append(ref)
    manifest = WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=roots,
        nodes=list(roots),
        created_by_run=RUN,
        updated_at=NOW,
    )
    assert len(manifest.roots) == 16
    assert len(manifest.nodes) == 16


def test_manifest_rejects_too_many_nodes() -> None:
    nodes = [
        _owned(node_id=f"n_{i}", path=f"/obj/n{i}", parent_path="/obj") for i in range(4097)
    ]
    with pytest.raises((ValueError, TypeError)):
        WorkspaceManifest.build(
            workspace_id=WS,
            session_id=SES,
            instance_id="hou_instance_1",
            scene_epoch=1,
            roots=[nodes[0]],
            nodes=nodes,
            created_by_run=RUN,
            updated_at=NOW,
        )


def test_manifest_revision_ignores_updated_at() -> None:
    first = _manifest(updated_at=NOW)
    second = _manifest(updated_at=LATER)
    assert first.revision == second.revision


def test_manifest_rename_keeps_node_id_but_changes_revision() -> None:
    original = _manifest()
    moved_nodes = [
        _owned(
            node_id=ref.node_id,
            path="/obj/ws/renamed" if ref.node_id == "n_child_0" else ref.path,
            node_type=ref.node_type,
            parent_path=ref.parent_path,
            capability=ref.capability,
            role=ref.role,
        )
        for ref in original.nodes
    ]
    moved = WorkspaceManifest.build(
        workspace_id=original.workspace_id,
        session_id=original.session_id,
        instance_id=original.instance_id,
        scene_epoch=original.scene_epoch,
        roots=list(original.roots),
        nodes=moved_nodes,
        created_by_run=original.created_by_run,
        updated_at=NOW,
    )
    assert {n.node_id for n in moved.nodes} == {
        n.node_id for n in original.nodes
    }
    assert moved.revision != original.revision


def test_manifest_compute_revision_matches_constructor() -> None:
    roots, nodes = _roots_nodes()
    digest = WorkspaceManifest.compute_revision(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        created_by_run=RUN,
        roots=roots,
        nodes=nodes,
    )
    manifest = _manifest()
    # same identity facts -> same revision regardless of input list type
    assert digest == manifest.revision


def test_manifest_rejects_naive_updated_at() -> None:
    with pytest.raises((TypeError, ValueError), match="timezone"):
        _manifest(updated_at=datetime(2026, 7, 15))


def test_manifest_normalizes_non_utc_updated_at() -> None:
    aware = datetime(2026, 7, 15, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    manifest = _manifest(updated_at=aware)
    assert manifest.updated_at == datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_manifest_deep_freezes_inputs() -> None:
    roots, nodes = _roots_nodes()
    roots_list = list(roots)
    nodes_list = list(nodes)
    manifest = WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=roots_list,
        nodes=nodes_list,
        created_by_run=RUN,
        updated_at=NOW,
    )
    revision_before = manifest.revision
    roots_list.append(_owned(node_id="n_x", path="/obj/x", parent_path="/obj"))
    nodes_list.append(_owned(node_id="n_y", path="/obj/y", parent_path="/obj"))
    assert manifest.revision == revision_before
    assert len(manifest.roots) == 1
    assert len(manifest.nodes) == 2


def test_manifest_to_dict_returns_fresh_tree() -> None:
    manifest = _manifest()
    first = manifest.to_dict()
    first["nodes"].append(first["nodes"][0])  # type: ignore[union-attr]
    first["roots"][0]["path"] = "/mutated"  # type: ignore[index]
    second = manifest.to_dict()
    assert len(second["nodes"]) == len(manifest.nodes)
    assert second["roots"][0]["path"] == "/obj/ws"  # type: ignore[index]


def test_manifest_rejects_wrong_id_kinds() -> None:
    with pytest.raises((TypeError, ValueError)):
        _manifest(workspace_id=SES)
    with pytest.raises((TypeError, ValueError)):
        _manifest(session_id=RUN)


# --------------------------------------------------------------------------
# Operations: tags, effects, parm values
# --------------------------------------------------------------------------


def test_operation_kind_and_effect_tags() -> None:
    assert _create().kind == "node.create"
    assert _create().effect == Effect.NODE_CREATE
    assert _setparm().kind == "parm.set"
    assert _setparm().effect == Effect.PARM_SET
    assert _connect().kind == "wire.connect"
    assert _connect().effect == Effect.WIRE_CONNECT


def test_create_node_requires_owned_workspace_id() -> None:
    with pytest.raises((TypeError, ValueError)):
        _create(workspace_id=None)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        _create(workspace_id="bad")


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, 0),
        (True, True),
        (1.5, 1.5),
        ("hello", "hello"),
        ((1, 2, 3), (1, 2, 3)),
        ([1, 2], (1, 2)),  # list normalized to tuple
    ],
    ids=["int", "bool", "float", "str", "tuple", "list-to-tuple"],
)
def test_setparm_accepts_scalar_and_homogeneous_tuple(
    value: object, expected: object
) -> None:
    op = _setparm(value=value, expected_old_value=value)
    assert op.value == expected


@pytest.mark.parametrize(
    "value",
    [None, {1: 2}, (1, (2,)), b"bytes", (1, "a"), (), (1, True)],
    ids=["none", "dict", "nested", "bytes", "hetero", "empty", "bool-int-mix"],
)
def test_setparm_rejects_invalid_value(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=value, expected_old_value=value)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "inf", "-inf"]
)
def test_setparm_rejects_non_finite_float(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=value, expected_old_value=value)


def test_setparm_rejects_oversize_string_value() -> None:
    huge = "x" * (16 * 1024 + 1)
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=huge, expected_old_value=huge)


def test_setparm_value_and_old_value_shape_must_match() -> None:
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=1, expected_old_value=(1,))  # scalar vs tuple
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=(1, 2), expected_old_value=(3,))  # length mismatch
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=(1, 2), expected_old_value=(3.0, 4.0))  # element type
    with pytest.raises((TypeError, ValueError)):
        _setparm(value=True, expected_old_value=1)  # bool vs int


def test_setparm_deep_freezes_tuple_value() -> None:
    source = [1, 2, 3]
    op = _setparm(value=source, expected_old_value=[1, 2, 3])
    source.append(99)
    assert op.value == (1, 2, 3)


def test_setparm_to_dict_emits_list_for_tuple() -> None:
    op = _setparm(value=(1, 2), expected_old_value=(3, 4))
    data = op.to_dict()
    assert data["value"] == [1, 2]
    assert data["expected_old_value"] == [3, 4]


def test_connect_input_indices_and_old_source() -> None:
    op = _connect(input_index=2, source_output_index=1, expected_old_source=_wireref())
    assert op.input_index == 2
    assert op.source_output_index == 1
    assert isinstance(op.expected_old_source, WireRef)
    assert op.to_dict()["expected_old_source"]["source_output_index"] == 0


@pytest.mark.parametrize("index", [-1, True, 1.0, "0"])
def test_connect_input_rejects_bad_index(index: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _connect(input_index=index)  # type: ignore[arg-type]


def test_connect_input_rejects_non_noderef_target_or_source() -> None:
    with pytest.raises((TypeError, ValueError)):
        _connect(target="/obj")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        _connect(source="/obj")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------


def test_condition_kind_tags() -> None:
    assert SceneBindingEquals(instance_id="hou_1", scene_epoch=1).kind == "scene.binding_equals"
    assert WorkspaceRevisionEquals(workspace_id=WS, revision=REVISION).kind == "workspace.revision_equals"
    assert NodeIdentityEquals(node=_noderef()).kind == "node.identity_equals"
    assert ParmValueEquals(target=_noderef(), parm_name="tx", value=1).kind == "parm.value_equals"
    assert WireInputEquals(target=_noderef(), input_index=0, source=None).kind == "wire.input_equals"
    assert NodeAbsent(path="/obj/new", node_id="n_new").kind == "node.absent"


def test_parm_value_equals_validates_value_contract() -> None:
    with pytest.raises((TypeError, ValueError)):
        ParmValueEquals(target=_noderef(), parm_name="tx", value=None)
    with pytest.raises((TypeError, ValueError)):
        ParmValueEquals(target=_noderef(), parm_name="tx", value={})


# --------------------------------------------------------------------------
# RiskSummary / CheckpointPlan
# --------------------------------------------------------------------------


def test_risk_summary_validates_effect_names() -> None:
    with pytest.raises((TypeError, ValueError)):
        _risk(effect_names=("node.delete",))
    with pytest.raises((TypeError, ValueError)):
        _risk(effect_names=("not.an.effect",))


def test_risk_summary_rejects_bad_types() -> None:
    with pytest.raises((TypeError, ValueError)):
        _risk(operation_count=True)
    with pytest.raises((TypeError, ValueError)):
        _risk(touches_external_nodes="no")  # type: ignore[arg-type]


def test_risk_summary_sorts_and_dedupes_effect_names() -> None:
    risk = _risk(effect_names=("parm.set", "node.create", "parm.set"))
    assert risk.effect_names == ("node.create", "parm.set")


def test_checkpoint_plan_validates_entries() -> None:
    plan = CheckpointPlan(
        nodes=[_noderef()],
        parameters=[ParmSnapshot(target=_noderef(), parm_name="tx")],
        wires=[WireSnapshot(target=_noderef(), input_index=0)],
    )
    assert len(plan.nodes) == 1
    with pytest.raises((TypeError, ValueError)):
        ParmSnapshot(target="/obj", parm_name="tx")  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        WireSnapshot(target=_noderef(), input_index=-1)


# --------------------------------------------------------------------------
# ChangeSet
# --------------------------------------------------------------------------


def test_changeset_builds_and_exposes_digest() -> None:
    cs = _changeset()
    assert cs.schema_version == 1
    assert len(cs.digest) == 64
    assert cs.digest == cs.digest  # stable


def test_changeset_digest_excludes_digest_field() -> None:
    cs = _changeset()
    assert "digest" not in cs.to_dict()


def test_changeset_digest_stable_across_construction() -> None:
    a = _changeset()
    b = _changeset()
    assert a.digest == b.digest


def test_changeset_digest_changes_with_covered_field() -> None:
    base = _changeset()
    changed_created = _changeset(created_at=LATER)
    assert base.digest != changed_created.digest
    changed_parm = _changeset(operations=(_setparm(value=1, expected_old_value=0),))
    assert base.digest != changed_parm.digest


def test_changeset_digest_includes_created_at() -> None:
    a = _changeset(created_at=NOW)
    b = _changeset(created_at=LATER)
    assert a.digest != b.digest


def test_changeset_rejects_duplicate_op_ids() -> None:
    with pytest.raises((TypeError, ValueError), match="op_id"):
        _changeset(
            operations=(
                _setparm(op_id="dup"),
                _setparm(op_id="dup", parm_name="ty"),
            )
        )


def test_changeset_rejects_empty_or_too_many_operations() -> None:
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=())
    too_many = tuple(_setparm(op_id=f"op_{i}", parm_name=f"p{i}") for i in range(257))
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=too_many)


# F7-1: distinct operation IDs must not make a created node id or derived
# create path repeatable within a ChangeSet.


def test_changeset_rejects_duplicate_created_node_id() -> None:
    create_a = _create(op_id="op_a", node_id="n_new", node_name="geo_new")
    create_b = _create(op_id="op_b", node_id="n_new", node_name="other")
    with pytest.raises((TypeError, ValueError), match="created"):
        _changeset(operations=(create_a, create_b))


def test_changeset_rejects_duplicate_created_derived_path() -> None:
    # distinct node ids but the same derived path (same parent + node name)
    create_a = _create(op_id="op_a", node_id="n_a", node_name="dup")
    create_b = _create(op_id="op_b", node_id="n_b", node_name="dup")
    with pytest.raises((TypeError, ValueError), match="created"):
        _changeset(operations=(create_a, create_b))


def test_changeset_accepts_distinct_created_node_ids_and_paths() -> None:
    create_a = _create(op_id="op_a", node_id="n_a", node_name="a")
    create_b = _create(op_id="op_b", node_id="n_b", node_name="b")
    cs = _changeset(
        operations=(create_a, create_b),
        affected_nodes=(
            _noderef(node_id="n_a", path="/obj/ws/a"),
            _noderef(node_id="n_b", path="/obj/ws/b"),
        ),
        risk_summary=_risk(
            effect_names=("node.create",),
            affected_paths=("/obj/ws/a", "/obj/ws/b"),
            operation_count=2,
        ),
    )
    assert len(cs.operations) == 2


# --------------------------------------------------------------------------
# D1: intra-ChangeSet created-reference forward-sequence validation
# --------------------------------------------------------------------------


def _d1_created_ref(node_id: str = "n_new", name: str = "geo_new", **kw: object) -> NodeRef:
    """The exact derived NodeRef for a CreateNode with the given fields."""
    values: dict[str, object] = dict(
        node_id=node_id, path=f"/obj/ws/{name}",
        expected_type="geo", expected_workspace_id=WS,
    )
    values.update(kw)
    return NodeRef(**values)  # type: ignore[arg-type]


def test_d1_create_then_set_accepted() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    setparm = _setparm(target=ref, op_id="op_s")
    cs = _changeset(operations=(create, setparm))
    assert len(cs.operations) == 2


def test_d1_create_then_connect_target_accepted() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    connect = _connect(target=ref, op_id="op_w")
    cs = _changeset(operations=(create, connect))
    assert len(cs.operations) == 2


def test_d1_create_under_created_parent_accepted() -> None:
    create_a = _create(op_id="op_a", node_id="n_a", node_name="a")
    parent_ref = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = _create(op_id="op_b", node_id="n_b", node_name="b", parent=parent_ref,
                       node_type="geo")
    cs = _changeset(operations=(create_a, create_b))
    assert len(cs.operations) == 2


def test_d1_multi_level_chain_accepted() -> None:
    create_a = _create(op_id="op_a", node_id="n_a", node_name="a")
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = _create(op_id="op_b", node_id="n_b", node_name="b", parent=ref_a, node_type="geo")
    ref_b = NodeRef(node_id="n_b", path="/obj/ws/a/b", expected_type="geo", expected_workspace_id=WS)
    setparm = _setparm(target=ref_b, op_id="op_s")
    cs = _changeset(operations=(create_a, create_b, setparm))
    assert len(cs.operations) == 3


def test_d1_forward_set_target_rejected() -> None:
    ref = _d1_created_ref()
    setparm = _setparm(target=ref, op_id="op_s")
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(setparm, create))


def test_d1_forward_connect_source_rejected() -> None:
    ref = _d1_created_ref()
    connect = _connect(source=ref, op_id="op_w")
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(connect, create))


def test_d1_forward_create_parent_rejected() -> None:
    parent_ref = _d1_created_ref()
    create_child = _create(op_id="op_child", node_id="n_b", node_name="b", parent=parent_ref,
                           node_type="geo")
    create_parent = _create(op_id="op_parent", node_id="n_new", node_name="geo_new")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create_child, create_parent))


def test_d1_cycle_rejected() -> None:
    ref_a = NodeRef(node_id="n_b", path="/obj/ws/b", expected_type="geo", expected_workspace_id=WS)
    ref_b = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_a = _create(op_id="op_a", node_id="n_a", node_name="a", parent=ref_a, node_type="geo")
    create_b = _create(op_id="op_b", node_id="n_b", node_name="b", parent=ref_b, node_type="geo")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create_a, create_b))


def test_d1_wrong_created_path_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = NodeRef(node_id="n_new", path="/obj/ws/WRONG", expected_type="geo", expected_workspace_id=WS)
    setparm = _setparm(target=wrong, op_id="op_s")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create, setparm))


def test_d1_wrong_created_type_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="WRONG", expected_workspace_id=WS)
    setparm = _setparm(target=wrong, op_id="op_s")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create, setparm))


def test_d1_wrong_created_workspace_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS2)
    setparm = _setparm(target=wrong, op_id="op_s")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create, setparm))


def test_d1_created_path_wrong_id_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = NodeRef(node_id="n_other", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    setparm = _setparm(target=wrong, op_id="op_s")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create, setparm))


def test_d1_read_deps_wrong_path_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = _noderef(node_id="n_new", path="/obj/ws/WRONG")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), read_dependencies=(wrong,))


def test_d1_precondition_wrong_path_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = _noderef(node_id="n_new", path="/obj/ws/WRONG")
    cond = NodeIdentityEquals(node=wrong)
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), preconditions=(cond,))


def test_d1_checkpoint_wrong_path_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = _noderef(node_id="n_new", path="/obj/ws/WRONG")
    plan = CheckpointPlan(nodes=(wrong,), parameters=(), wires=())
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), checkpoint_plan=plan)


def test_d1_impossible_parm_precondition_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    cond = ParmValueEquals(target=ref, parm_name="tx", value=0)
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), preconditions=(cond,))


def test_d1_expected_old_source_created_exact_match_accepted() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    old_source = WireRef(source=ref, source_output_index=0)
    connect = _connect(target=ref, expected_old_source=old_source, op_id="op_w")
    cs = _changeset(operations=(create, connect))
    assert len(cs.operations) == 2


def test_d1_expected_old_source_created_wrong_path_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    wrong = NodeRef(node_id="n_new", path="/obj/ws/WRONG", expected_type="geo", expected_workspace_id=WS)
    old_source = WireRef(source=wrong, source_output_index=0)
    connect = _connect(target=_noderef(node_id="n_child"), expected_old_source=old_source, op_id="op_w")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create, connect))


def test_d1_expected_old_source_forward_ref_to_later_create_rejected() -> None:
    """expected_old_source.source pointing forward to a later create is rejected."""
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    old_source = WireRef(source=ref, source_output_index=0)
    # connect BEFORE create → forward ref through expected_old_source
    connect = _connect(target=_noderef(node_id="n_child"), expected_old_source=old_source, op_id="op_w")
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(connect, create))


def test_d1_impossible_identity_precondition_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    cond = NodeIdentityEquals(node=ref)
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), preconditions=(cond,))


def test_d1_impossible_wire_precondition_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    cond = WireInputEquals(target=ref, input_index=0, source=None)
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), preconditions=(cond,))


def test_d1_impossible_identity_postcondition_for_created_accepted() -> None:
    """Postconditions CAN reference a created node (it exists after the write)."""
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    cond = NodeIdentityEquals(node=ref)
    cs = _changeset(operations=(create,), expected_postconditions=(cond,))
    assert len(cs.operations) == 1


def test_d1_impossible_checkpoint_parm_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    plan = CheckpointPlan(nodes=(), parameters=(ParmSnapshot(target=ref, parm_name="tx"),), wires=())
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), checkpoint_plan=plan)


def test_d1_impossible_checkpoint_wire_for_created_rejected() -> None:
    create = _create(op_id="op_c", node_id="n_new", node_name="geo_new")
    ref = _d1_created_ref()
    plan = CheckpointPlan(nodes=(), parameters=(), wires=(WireSnapshot(target=ref, input_index=0),))
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(create,), checkpoint_plan=plan)


def test_changeset_rejects_duplicate_affected_nodes() -> None:
    target = _noderef()
    with pytest.raises((TypeError, ValueError)):
        _changeset(
            operations=(_setparm(),),
            affected_nodes=(target, target),
            risk_summary=_risk(
                effect_names=("parm.set",),
                affected_paths=("/obj/ws/geo1",),
            ),
        )


# F1 regression: affected_nodes / read_dependencies must be unique by stable
# node identity (node_id when present, otherwise path), rejecting the same
# identity paired with conflicting path/type/workspace facts.


def test_changeset_rejects_same_node_id_with_different_path_in_affected() -> None:
    first = _noderef(node_id="n_child", path="/obj/ws/geo1")
    second = _noderef(node_id="n_child", path="/obj/ws/renamed")
    with pytest.raises((TypeError, ValueError)):
        _changeset(affected_nodes=(first, second))


def test_changeset_rejects_same_node_id_with_different_type_in_affected() -> None:
    first = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_type="geo")
    second = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_type="xform")
    with pytest.raises((TypeError, ValueError)):
        _changeset(affected_nodes=(first, second))


def test_changeset_rejects_same_path_with_different_node_id_in_affected() -> None:
    first = _noderef(node_id="n_a", path="/obj/ws/geo1")
    second = _noderef(node_id="n_b", path="/obj/ws/geo1")
    with pytest.raises((TypeError, ValueError)):
        _changeset(affected_nodes=(first, second))


def test_changeset_rejects_same_node_id_with_different_workspace_in_affected() -> None:
    first = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_workspace_id=WS)
    second = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_workspace_id=WS2)
    with pytest.raises((TypeError, ValueError)):
        _changeset(affected_nodes=(first, second))


def test_changeset_rejects_same_node_id_with_different_path_in_read_dependencies() -> None:
    first = _noderef(node_id="n_dep", path="/obj/ws/d1")
    second = _noderef(node_id="n_dep", path="/obj/ws/d2")
    with pytest.raises((TypeError, ValueError)):
        _changeset(read_dependencies=(first, second))


def test_changeset_rejects_same_path_with_different_node_id_in_read_dependencies() -> None:
    first = _noderef(node_id="n_da", path="/obj/ws/dep")
    second = _noderef(node_id="n_db", path="/obj/ws/dep")
    with pytest.raises((TypeError, ValueError)):
        _changeset(read_dependencies=(first, second))


def test_changeset_accepts_distinct_owned_identities_in_affected() -> None:
    # distinct node_ids at distinct paths are a valid affected set
    cs = _changeset(
        operations=(_setparm(),),
        affected_nodes=(
            _noderef(node_id="n_child", path="/obj/ws/geo1"),
            _noderef(node_id="n_other", path="/obj/ws/other"),
        ),
        risk_summary=_risk(
            effect_names=("parm.set",), affected_paths=("/obj/ws/geo1",)
        ),
    )
    assert len(cs.affected_nodes) == 2


def test_changeset_enforces_node_reference_limit() -> None:
    affected = tuple(
        _noderef(node_id=f"n_{i}", path=f"/obj/n{i}") for i in range(4097)
    )
    with pytest.raises((TypeError, ValueError)):
        _changeset(
            operations=(_setparm(),),
            affected_nodes=affected,
            risk_summary=_risk(
                effect_names=("parm.set",),
                affected_paths=tuple(f"/obj/n{i}" for i in range(4097)),
            ),
        )


def test_changeset_rejects_wrong_id_kinds() -> None:
    with pytest.raises((TypeError, ValueError)):
        _changeset(change_id=SES)
    with pytest.raises((TypeError, ValueError)):
        _changeset(session_id=RUN)
    with pytest.raises((TypeError, ValueError)):
        _changeset(run_id=SES)


def test_changeset_rejects_wrong_permission_enum() -> None:
    with pytest.raises((TypeError, ValueError)):
        _changeset(required_permission="OwnedWorkspace")  # type: ignore[arg-type]


def test_changeset_rejects_bad_base_revision() -> None:
    with pytest.raises((TypeError, ValueError)):
        _changeset(base_revision="not-a-hash")


def test_changeset_rejects_naive_created_at() -> None:
    with pytest.raises((TypeError, ValueError), match="timezone"):
        _changeset(created_at=datetime(2026, 7, 15))


def test_changeset_rejects_non_operation_entries() -> None:
    with pytest.raises((TypeError, ValueError)):
        _changeset(operations=(_noderef(),))  # type: ignore[arg-type]


def test_changeset_rejects_duplicate_precondition_identity() -> None:
    cond = ParmValueEquals(target=_noderef(), parm_name="tx", value=1)
    with pytest.raises((TypeError, ValueError), match="(duplicate|contradict)"):
        _changeset(preconditions=(cond, cond))


def test_changeset_rejects_contradictory_conditions() -> None:
    # same target/parm but different value -> contradiction
    c1 = ParmValueEquals(target=_noderef(), parm_name="tx", value=1)
    c2 = ParmValueEquals(target=_noderef(), parm_name="tx", value=2)
    with pytest.raises((TypeError, ValueError), match="(duplicate|contradict)"):
        _changeset(preconditions=(c1, c2))


def test_changeset_rejects_node_absent_and_present_contradiction() -> None:
    target = _noderef(node_id="n_new", path="/obj/ws/geo_new")
    with pytest.raises((TypeError, ValueError), match="(duplicate|contradict)"):
        _changeset(
            preconditions=(
                NodeAbsent(path="/obj/ws/geo_new", node_id="n_new"),
                NodeIdentityEquals(node=target),
            )
        )


def test_changeset_rejects_oversize_canonical_payload() -> None:
    # Each value stays well under the 16 KiB parm cap, but 256 operations push
    # the canonical ChangeSet payload past the 128 KiB limit.
    value = "x" * 512
    ops = tuple(
        _setparm(op_id=f"op_{i}", parm_name=f"p{i}", value=value, expected_old_value=value)
        for i in range(256)
    )
    with pytest.raises((TypeError, ValueError), match="(payload|size|limit)"):
        _changeset(operations=ops)


def test_changeset_deep_freezes_collections() -> None:
    affected = [_noderef()]
    precond = [NodeIdentityEquals(node=_noderef())]
    cs = _changeset(
        operations=(_setparm(),),
        affected_nodes=affected,
        preconditions=precond,
        risk_summary=_risk(effect_names=("parm.set",), affected_paths=("/obj/ws/geo1",)),
    )
    digest_before = cs.digest
    affected.append(_noderef(node_id="n_extra", path="/obj/extra"))
    precond.append(NodeIdentityEquals(node=_noderef(node_id="n_p", path="/obj/p")))
    assert cs.digest == digest_before
    assert len(cs.affected_nodes) == 1
    assert len(cs.preconditions) == 1


def test_changeset_to_dict_returns_fresh_tree() -> None:
    cs = _changeset()
    first = cs.to_dict()
    first["change_id"] = "mutated"
    first["operations"][0]["op_id"] = "mutated"  # type: ignore[index]
    second = cs.to_dict()
    assert second["change_id"] == CHG
    assert second["operations"][0]["op_id"] == "op_create"  # type: ignore[index]


# --------------------------------------------------------------------------
# ApprovalRecord state-dependent nullability
# --------------------------------------------------------------------------


def _approval(**overrides: object) -> ApprovalRecord:
    values: dict[str, object] = dict(
        approval_id=APR,
        change_id=CHG,
        changeset_digest=REVISION,
        decision=ApprovalDecision.PENDING,
        decided_by=None,
        requested_at=NOW,
        decided_at=None,
        expires_at=LATER,
        approved_instance_id=None,
        approved_scene_epoch=None,
    )
    values.update(overrides)
    return ApprovalRecord(**values)  # type: ignore[arg-type]


def test_approval_pending_has_no_decision_facts() -> None:
    rec = _approval()
    assert rec.decided_at is None
    assert rec.decided_by is None
    assert rec.approved_instance_id is None
    assert rec.approved_scene_epoch is None
    assert rec.schema_version == 1


def test_approval_pending_rejects_decision_facts() -> None:
    with pytest.raises((TypeError, ValueError)):
        _approval(decided_at=LATER)
    with pytest.raises((TypeError, ValueError)):
        _approval(decided_by="local_user")


def test_approval_approved_binds_instance_and_epoch() -> None:
    rec = _approval(
        decision=ApprovalDecision.APPROVED,
        decided_by="local_user",
        decided_at=LATER,
        approved_instance_id="hou_instance_1",
        approved_scene_epoch=1,
    )
    assert rec.decided_at == LATER
    assert rec.approved_instance_id == "hou_instance_1"


def test_approval_approved_requires_binding() -> None:
    with pytest.raises((TypeError, ValueError)):
        _approval(
            decision=ApprovalDecision.APPROVED,
            decided_by="local_user",
            decided_at=LATER,
            approved_instance_id=None,
            approved_scene_epoch=None,
        )


def test_approval_rejected_has_decision_no_binding() -> None:
    rec = _approval(
        decision=ApprovalDecision.REJECTED,
        decided_by="local_user",
        decided_at=LATER,
    )
    assert rec.approved_instance_id is None
    # rejected must not bind scene facts
    with pytest.raises((TypeError, ValueError)):
        _approval(
            decision=ApprovalDecision.REJECTED,
            decided_by="local_user",
            decided_at=LATER,
            approved_instance_id="hou_instance_1",
            approved_scene_epoch=1,
        )


def test_approval_expired_allows_null_decided_by() -> None:
    rec = _approval(decision=ApprovalDecision.EXPIRED, decided_at=LATER)
    assert rec.decided_by is None


def test_approval_rejects_decided_by_other_than_local_user() -> None:
    with pytest.raises((TypeError, ValueError)):
        _approval(
            decision=ApprovalDecision.APPROVED,
            decided_by="someone_else",
            decided_at=LATER,
            approved_instance_id="hou_1",
            approved_scene_epoch=1,
        )


def test_approval_expiry_must_be_after_request() -> None:
    with pytest.raises((TypeError, ValueError), match="expir"):
        _approval(expires_at=NOW)
    with pytest.raises((TypeError, ValueError), match="expir"):
        _approval(requested_at=LATER, expires_at=NOW)


def test_approval_rejects_wrong_id_kind() -> None:
    with pytest.raises((TypeError, ValueError)):
        _approval(approval_id=CHG)


def test_approval_rejects_bad_digest() -> None:
    with pytest.raises((TypeError, ValueError)):
        _approval(changeset_digest="not-a-hash")


# --------------------------------------------------------------------------
# ChangeReceipt invariants
# --------------------------------------------------------------------------


def _result(passed: bool = True, kind: str = "parm.value_equals") -> ConditionResult:
    return ConditionResult(kind=kind, passed=passed)


def _receipt(**overrides: object) -> ChangeReceipt:
    values: dict[str, object] = dict(
        change_id=CHG,
        status=ReceiptStatus.APPLIED,
        instance_id="hou_instance_1",
        scene_epoch=1,
        before_revision=REVISION,
        after_revision="b" * 64,
        applied_op_ids=("op_create",),
        postcondition_results=(_result(True),),
        rollback_results=(),
        scene_may_have_changed=False,
        completed_at=LATER,
    )
    values.update(overrides)
    return ChangeReceipt(**values)  # type: ignore[arg-type]


def test_receipt_applied_requires_passing_postconditions() -> None:
    rec = _receipt()
    assert rec.is_success is True
    with pytest.raises((TypeError, ValueError)):
        _receipt(postcondition_results=(_result(False),))


def test_receipt_applied_requires_scene_unchanged() -> None:
    with pytest.raises((TypeError, ValueError)):
        _receipt(scene_may_have_changed=True)


def test_receipt_already_applied_invariants() -> None:
    rec = _receipt(status=ReceiptStatus.ALREADY_APPLIED)
    assert rec.is_success is True
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ALREADY_APPLIED, postcondition_results=(_result(False),))


def test_receipt_rolled_back_requires_passing_rollback() -> None:
    rec = _receipt(
        status=ReceiptStatus.ROLLED_BACK,
        postcondition_results=(),
        rollback_results=(_result(True),),
        after_revision=REVISION,
    )
    assert rec.is_success is False
    with pytest.raises((TypeError, ValueError)):
        _receipt(
            status=ReceiptStatus.ROLLED_BACK,
            postcondition_results=(),
            rollback_results=(_result(False),),
        )


def test_receipt_partial_and_critical_require_scene_changed() -> None:
    for status in (ReceiptStatus.PARTIAL, ReceiptStatus.CRITICAL_RECOVERY):
        rec = _receipt(status=status, scene_may_have_changed=True, postcondition_results=())
        assert rec.is_success is False
        with pytest.raises((TypeError, ValueError)):
            _receipt(status=status, scene_may_have_changed=False)


def test_receipt_rejects_wrong_id_kind_and_bad_revision() -> None:
    with pytest.raises((TypeError, ValueError)):
        _receipt(change_id=APR)
    with pytest.raises((TypeError, ValueError)):
        _receipt(before_revision="bad")


# --------------------------------------------------------------------------
# B-1: optional apply-error fields on ChangeReceipt
# --------------------------------------------------------------------------


def test_receipt_applied_omits_error_fields_from_to_dict() -> None:
    rec = _receipt()
    payload = rec.to_dict()
    assert "error_code" not in payload
    assert "error_message" not in payload


def test_receipt_rolled_back_carries_error_fields_round_trip() -> None:
    rec = _receipt(
        status=ReceiptStatus.ROLLED_BACK,
        postcondition_results=(),
        rollback_results=(),
        after_revision=REVISION,
        applied_op_ids=(),
        error_code="houdini.operation_failed",
        error_message="Cannot create node 'copytopoints::2.0'",
    )
    payload = rec.to_dict()
    assert payload["error_code"] == "houdini.operation_failed"
    assert payload["error_message"] == "Cannot create node 'copytopoints::2.0'"
    assert rec.error_code == "houdini.operation_failed"
    assert rec.error_message == "Cannot create node 'copytopoints::2.0'"


def test_receipt_applied_rejects_error_fields() -> None:
    # Success states must never carry a cause; consumers treat APPLIED as
    # authoritative success.
    with pytest.raises((TypeError, ValueError)):
        _receipt(error_code="houdini.operation_failed", error_message="boom")
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ALREADY_APPLIED,
                 error_code="houdini.operation_failed", error_message="boom")


def test_receipt_error_code_must_be_dotted_lowercase() -> None:
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_code="UPPER", error_message="x")
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_code="houdini.", error_message="x")
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_code="houdini.123bad", error_message="x")


def test_receipt_error_message_without_code_is_rejected() -> None:
    # Downstream consumers dispatch on codes, not prose. A message without
    # a code would orphan the cause from any structured handling.
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_message="boom")


def test_receipt_error_code_alone_is_allowed() -> None:
    # Code without message: the cause may be obvious from the code itself.
    rec = _receipt(
        status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
        rollback_results=(), after_revision=REVISION,
        error_code="bridge.stale_scene", error_message=None,
    )
    payload = rec.to_dict()
    assert payload["error_code"] == "bridge.stale_scene"
    assert "error_message" not in payload


def test_receipt_error_message_is_length_bounded() -> None:
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_code="houdini.operation_failed",
                 error_message="x" * 5000)


def test_receipt_error_message_rejects_control_chars() -> None:
    with pytest.raises((TypeError, ValueError)):
        _receipt(status=ReceiptStatus.ROLLED_BACK, applied_op_ids=(),
                 rollback_results=(), after_revision=REVISION,
                 error_code="houdini.operation_failed",
                 error_message="bad\nmultiline\tmessage")


def test_condition_result_rejects_bad_types() -> None:
    with pytest.raises((TypeError, ValueError)):
        ConditionResult(kind="parm.value_equals", passed="yes")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# import + public-surface hygiene
# --------------------------------------------------------------------------


_FORBIDDEN_IMPORT_ROOTS = {"hou", "rpyc", "subprocess", "os", "shutil", "sqlite3"}
_FORBIDDEN_IMPORT_PREFIXES = ("eee_agent.bridge",)


def _imported_modules(module_name: str) -> set[str]:
    module = __import__(module_name, fromlist=["x"])
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("module_name", ["eee_agent.changesets.contracts", "eee_agent.changesets.policy"])
def test_changesets_modules_do_not_import_forbidden(module_name: str) -> None:
    names = _imported_modules(module_name)
    for name in names:
        root = name.split(".")[0]
        assert root not in _FORBIDDEN_IMPORT_ROOTS, f"forbidden import: {name}"
        for prefix in _FORBIDDEN_IMPORT_PREFIXES:
            assert not (name == prefix or name.startswith(prefix + ".")), (
                f"forbidden import: {name}"
            )
        assert "transport" not in name, f"forbidden transport import: {name}"


def test_changesets_package_imports_without_heavy_deps() -> None:
    """A fresh interpreter importing the package must not load hou/rpyc.

    Uses an isolated subprocess so the assertion is independent of whatever
    other tests in the full suite may have imported ``rpyc`` into this
    process's ``sys.modules`` already.
    """
    import json
    import subprocess
    import sys

    code = (
        "import sys, json; "
        "import eee_agent.changesets; "
        "print(json.dumps(sorted(k for k in ('hou', 'rpyc') if k in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == []


def test_forbidden_effect_and_operation_names_absent() -> None:
    import eee_agent.changesets as changesets

    allowed_effects = {"node.create", "parm.set", "wire.connect"}
    assert {e.value for e in changesets.Effect} == allowed_effects
    forbidden = {
        "node.delete",
        "node.disconnect",
        "code.install",
        "file.write",
        "scene.read",
        "user_data.set",
        "hip.save",
        "hda.install",
    }
    exported_names = set(dir(changesets))
    for name in forbidden:
        assert name not in allowed_effects
        assert name.replace(".", "_") not in exported_names


def test_policy_decision_is_constructible_and_immutable() -> None:
    decision = PolicyDecision(
        allowed=True,
        mode=PermissionMode.OWNED_WORKSPACE,
        normalized_effects=("node.create",),
        approval_required=True,
        backup_required=False,
        denial_codes=(),
        changeset_digest=REVISION,
    )
    assert decision.allowed is True
    assert decision.approval_required is True
    with pytest.raises(AttributeError):
        decision.allowed = False  # type: ignore[misc]
