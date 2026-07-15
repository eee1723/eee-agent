"""Task 16-A: deterministic pure ChangeSet policy engine tests.

Pure-Python RED/GREEN tests for :func:`eee_agent.changesets.policy.evaluate_policy`
and the :class:`PolicyDecision` it returns. No ``hou``, ``rpyc``, database,
transport, or Houdini process is exercised here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eee_agent.changesets import (
    ChangeSet,
    ConnectInput,
    CreateNode,
    Effect,
    NodeRef,
    OwnedNodeRef,
    PermissionMode,
    SetParm,
    WireRef,
    WorkspaceManifest,
    evaluate_policy,
)
from eee_agent.houdini_bridge.contracts import SceneBinding

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
WS2 = f"ws_{'3' * 32}"
CHG = f"chg_{'4' * 32}"
REVISION = "a" * 64
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
INSTANCE = "hou_instance_1"


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = dict(
        instance_id=INSTANCE, scene_epoch=1, hip_path=None, observed_revision="rev-1"
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


def _manifest() -> WorkspaceManifest:
    root = _owned()
    child = _owned(
        node_id="n_child", path="/obj/ws/geo1", parent_path="/obj/ws", role="member"
    )
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id=INSTANCE,
        scene_epoch=1,
        roots=[root],
        nodes=[root, child],
        created_by_run=RUN,
        updated_at=NOW,
    )


def _consistent_risk(
    ops: tuple[object, ...],
    affected_paths: tuple[str, ...],
    *,
    touches_external: bool = False,
    requires_backup: bool = False,
):
    from eee_agent.changesets import RiskSummary

    effects = sorted({op.effect.value for op in ops})  # type: ignore[attr-defined]
    return RiskSummary(
        touches_external_nodes=touches_external,
        changes_wiring=any(isinstance(op, ConnectInput) for op in ops),
        requires_backup=requires_backup,
        operation_count=len(ops),
        effect_names=tuple(effects),
        affected_paths=tuple(sorted(set(affected_paths))),
    )


def _changeset(
    ops: tuple[object, ...],
    *,
    affected: tuple[NodeRef, ...] = (),
    read_deps: tuple[NodeRef, ...] = (),
    workspace_id: str | None = WS,
    permission: PermissionMode = PermissionMode.OWNED_WORKSPACE,
    scoped_node_ids: tuple[str, ...] = (),
    affected_paths: tuple[str, ...] = (),
    touches_external: bool = False,
    requires_backup: bool = False,
    binding: SceneBinding | None = None,
) -> ChangeSet:
    from eee_agent.changesets import CheckpointPlan

    return ChangeSet(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=binding or _binding(),
        workspace_id=workspace_id,
        base_revision=REVISION,
        required_permission=permission,
        scoped_node_ids=scoped_node_ids,
        operations=ops,  # type: ignore[arg-type]
        affected_nodes=affected,
        read_dependencies=read_deps,
        preconditions=(),
        expected_postconditions=(),
        risk_summary=_consistent_risk(ops, affected_paths, touches_external=touches_external, requires_backup=requires_backup),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )


def _setparm(target: NodeRef | None = None, op_id: str = "op_parm") -> SetParm:
    return SetParm(
        op_id=op_id,
        target=target or _noderef(),
        parm_name="tx",
        value=1,
        expected_old_value=0,
    )


# --------------------------------------------------------------------------
# public surface + determinism
# --------------------------------------------------------------------------


def test_policy_echoes_digest_and_requires_approval() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.approval_required is True
    assert decision.changeset_digest == cs.digest
    assert decision.mode is PermissionMode.OWNED_WORKSPACE


def test_normalized_effects_sorted_deterministically() -> None:
    target = _noderef()
    # source is an owned workspace node so the wire is owned -> owned -> owned.
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    connect = ConnectInput(
        op_id="op_wire",
        target=target,
        input_index=0,
        source=source,
        source_output_index=0,
        expected_old_source=None,
    )
    parm = _setparm(target, op_id="op_parm")
    cs = _changeset(
        (parm, connect),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.normalized_effects == ("parm.set", "wire.connect")


def test_repeated_evaluation_is_equal_and_does_not_mutate() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
    )
    manifest = _manifest()
    first = evaluate_policy(cs, workspace=manifest)
    second = evaluate_policy(cs, workspace=manifest)
    assert first == second
    # inputs are untouched
    assert len(cs.operations) == 1
    assert len(manifest.nodes) == 2
    assert manifest.revision == _manifest().revision


# --------------------------------------------------------------------------
# OwnedWorkspace
# --------------------------------------------------------------------------


def test_owned_workspace_allows_owned_change() -> None:
    target = _noderef()
    cs = _changeset((_setparm(target),), affected=(target,), affected_paths=("/obj/ws/geo1",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_owned_workspace_denies_missing_manifest() -> None:
    target = _noderef()
    # No manifest => target ownership unprovable => external touch reported.
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is False
    assert "policy.missing_workspace" in decision.denial_codes


def test_owned_workspace_denies_ownership_mismatch() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        workspace_id=WS2,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_workspace_denies_external_target_mutation() -> None:
    external = _noderef(
        node_id="n_ext", path="/obj/external", expected_workspace_id=None
    )
    cs = _changeset(
        (_setparm(external),),
        affected=(external,),
        affected_paths=("/obj/external",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_workspace_denies_unowned_changed_target_as_ambiguous() -> None:
    # node_id None on a changed target => ownership cannot be confirmed.
    anonymous = NodeRef(node_id=None, path="/obj/ws/geo1", expected_type="geo", expected_workspace_id=None)
    cs = _changeset(
        (_setparm(anonymous),),
        affected=(anonymous,),
        affected_paths=("/obj/ws/geo1",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_ambiguous" in decision.denial_codes


def test_owned_workspace_denies_created_node_workspace_mismatch() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS2,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_new", path="/obj/ws/geo_new", expected_workspace_id=WS2),),
        affected_paths=("/obj/ws/geo_new",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_workspace_denies_stale_binding() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        binding=_binding(scene_epoch=99),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.stale_workspace" in decision.denial_codes


def test_owned_workspace_denies_locked_and_ambiguous_targets() -> None:
    target = _noderef()
    cs = _changeset((_setparm(target),), affected=(target,), affected_paths=("/obj/ws/geo1",))
    locked = evaluate_policy(cs, workspace=_manifest(), locked_node_paths={"/obj/ws/geo1"})
    assert locked.allowed is False
    assert "policy.locked_target" in locked.denial_codes
    ambiguous = evaluate_policy(cs, workspace=_manifest(), ambiguous_node_paths={"/obj/ws/geo1"})
    assert ambiguous.allowed is False
    assert "policy.ambiguous_target" in ambiguous.denial_codes


# --------------------------------------------------------------------------
# ScopedPatch
# --------------------------------------------------------------------------


def test_scoped_patch_allows_scoped_change() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_child",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_scoped_patch_denies_create() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_new", path="/obj/ws/geo_new"),),
        affected_paths=("/obj/ws/geo_new",),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_child",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.scope_violation" in decision.denial_codes


def test_scoped_patch_denies_target_outside_scope() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_other",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.scope_violation" in decision.denial_codes


def test_scoped_patch_scope_does_not_expand_via_affected() -> None:
    target = _noderef()
    # target is in affected_nodes but NOT in scoped_node_ids -> still denied.
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_other",),
        # affected_paths intentionally includes the target path
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False


def test_scoped_patch_denies_wire_endpoint_outside_scope() -> None:
    target = _noderef()
    # n_src is not a member of the workspace manifest, so it is external to it.
    source = _noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform")
    connect = ConnectInput(
        op_id="op_wire",
        target=target,
        input_index=0,
        source=source,
        source_output_index=0,
        expected_old_source=None,
    )
    cs = _changeset(
        (connect,),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws/src1"),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_child",),  # source n_src not scoped
        touches_external=True,  # n_src is external to the workspace manifest
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.scope_violation" in decision.denial_codes


# --------------------------------------------------------------------------
# ProjectChange
# --------------------------------------------------------------------------


def test_project_change_allows_enumerated_change() -> None:
    target = _noderef()
    # No manifest => the target's ownership cannot be proven, so it is external.
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_project_change_denies_omitted_affected_target() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_project_change_requires_created_node_in_affected() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(),
        affected_paths=("/obj/ws/geo_new",),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=True,  # no manifest => create parent ownership unprovable
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


# --------------------------------------------------------------------------
# backup + contradictions + ordering + malformed input
# --------------------------------------------------------------------------


def test_unavailable_backup_denies() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        requires_backup=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert decision.backup_required is True
    assert "policy.backup_unavailable" in decision.denial_codes


def test_effect_risk_contradiction_denies() -> None:
    target = _noderef()
    from eee_agent.changesets import RiskSummary, CheckpointPlan

    cs = ChangeSet(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(_setparm(target),),
        affected_nodes=(target,),
        read_dependencies=(),
        preconditions=(),
        expected_postconditions=(),
        risk_summary=RiskSummary(
            touches_external_nodes=False,
            changes_wiring=False,
            requires_backup=False,
            operation_count=1,
            effect_names=("node.create",),  # contradicts the parm.set operation
            affected_paths=("/obj/ws/geo1",),
        ),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_denial_codes_are_sorted_and_namespaced() -> None:
    target = _noderef()
    # missing workspace + backup required => at least two denials, must be sorted.
    # No manifest => target ownership unprovable => external touch reported.
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        requires_backup=True,
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.denial_codes == tuple(sorted(decision.denial_codes))
    assert all(code.startswith("policy.") for code in decision.denial_codes)
    assert len(decision.denial_codes) == len(set(decision.denial_codes))


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(locked_node_paths=["/obj", 1]),  # type: ignore[list-item]
        dict(ambiguous_node_paths=["/obj", 2]),  # type: ignore[list-item]
    ],
)
def test_policy_rejects_non_string_path_inputs(kwargs: dict[str, object]) -> None:
    target = _noderef()
    cs = _changeset((_setparm(target),), affected=(target,), affected_paths=("/obj/ws/geo1",))
    with pytest.raises((TypeError, ValueError)):
        evaluate_policy(cs, workspace=_manifest(), **kwargs)  # type: ignore[arg-type]


def test_policy_rejects_non_manifest_workspace() -> None:
    target = _noderef()
    cs = _changeset((_setparm(target),), affected=(target,), affected_paths=("/obj/ws/geo1",))
    with pytest.raises((TypeError, ValueError)):
        evaluate_policy(cs, workspace={"workspace_id": WS})  # type: ignore[arg-type]


def test_policy_rejects_non_changeset() -> None:
    with pytest.raises((TypeError, ValueError)):
        evaluate_policy({"change_id": CHG}, workspace=None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# F2: OwnedWorkspace must validate the full manifest identity of changed
# targets and owned wire sources (path + expected_type + workspace), not just
# node_id presence.
# --------------------------------------------------------------------------


def _wire(target: NodeRef, source: NodeRef, op_id: str = "op_wire") -> ConnectInput:
    return ConnectInput(
        op_id=op_id,
        target=target,
        input_index=0,
        source=source,
        source_output_index=0,
        expected_old_source=None,
    )


def test_owned_denies_changed_target_with_wrong_path() -> None:
    target = _noderef(node_id="n_child", path="/obj/ws/WRONG", expected_type="geo")
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/WRONG",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_denies_changed_target_with_wrong_type() -> None:
    target = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_type="WRONG")
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_denies_changed_target_with_wrong_workspace() -> None:
    target = _noderef(
        node_id="n_child", path="/obj/ws/geo1", expected_type="geo", expected_workspace_id=WS2
    )
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_denies_owned_wire_source_with_wrong_path() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws/WRONG", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws/WRONG"),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_denies_owned_wire_source_with_wrong_type() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="WRONG")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_denies_external_wire_source_not_in_read_dependencies() -> None:
    target = _noderef()
    source = NodeRef(
        node_id="n_ext", path="/obj/external", expected_type="geo", expected_workspace_id=None
    )
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.ownership_mismatch" in decision.denial_codes


def test_owned_allows_owned_wire_source_matching_manifest() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


# --------------------------------------------------------------------------
# F3: every changed target (parm target, both wire endpoints, created node)
# must be enumerated in affected_nodes for ALL permission modes.
# --------------------------------------------------------------------------


def test_owned_denies_omitted_parm_target_in_affected() -> None:
    target = _noderef()
    cs = _changeset((_setparm(target),), affected=(), affected_paths=("/obj/ws/geo1",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_scoped_denies_omitted_parm_target_in_affected() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.SCOPED_PATCH,
        scoped_node_ids=("n_child",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_omitted_wire_source_in_affected() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target,),  # source omitted
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_omitted_created_node_in_affected() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset((create,), affected=(), affected_paths=("/obj/ws/geo_new",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


# --------------------------------------------------------------------------
# F4: RiskSummary must not under/over-report external effects or affected paths.
# --------------------------------------------------------------------------


def test_owned_denies_external_source_underreports_touches_external() -> None:
    target = _noderef()
    external = NodeRef(
        node_id="n_ext", path="/obj/external", expected_type="geo", expected_workspace_id=None
    )
    cs = _changeset(
        (_wire(target, external),),
        affected=(target, external),
        read_deps=(external,),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        touches_external=False,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_owned_allows_external_source_with_correct_touches_external() -> None:
    target = _noderef()
    external = NodeRef(
        node_id="n_ext", path="/obj/external", expected_type="geo", expected_workspace_id=None
    )
    cs = _changeset(
        (_wire(target, external),),
        affected=(target, external),
        read_deps=(external,),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_owned_allows_internal_source_with_touches_external_false() -> None:
    target = _noderef()
    # n_root is owned by the manifest, so the wire touches no external node.
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
        touches_external=False,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_policy_denies_overreported_affected_path() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1", "/obj/extra"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_policy_denies_missing_create_derived_path() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_new", path="/obj/ws/geo_new"),),
        affected_paths=("/obj/ws/WRONG",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


@pytest.mark.parametrize(
    "permission",
    [PermissionMode.OWNED_WORKSPACE, PermissionMode.SCOPED_PATCH, PermissionMode.PROJECT_CHANGE],
)
def test_policy_denies_affected_path_contradiction_across_modes(
    permission: PermissionMode,
) -> None:
    target = _noderef()
    scoped = ("n_child",) if permission is PermissionMode.SCOPED_PATCH else ()
    workspace = None if permission is PermissionMode.PROJECT_CHANGE else _manifest()
    # ProjectChange has no manifest, so its target is external; owned/scoped target is internal.
    touches_external = permission is PermissionMode.PROJECT_CHANGE
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/WRONG",),
        permission=permission,
        scoped_node_ids=scoped,
        touches_external=touches_external,
    )
    decision = evaluate_policy(cs, workspace=workspace)
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


# --------------------------------------------------------------------------
# F5: external-touch derivation must include changed targets and fail closed
# without a manifest; with a manifest it must require exact fact matching
# (an ID-only match is insufficient).
# --------------------------------------------------------------------------


def test_project_change_denies_external_parm_target_underreported() -> None:
    target = _noderef()
    cs = _changeset(
        (_setparm(target),),
        affected=(target,),
        affected_paths=("/obj/ws/geo1",),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=False,  # no manifest => target ownership unprovable => external
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_project_change_denies_external_wire_source_underreported() -> None:
    # No manifest and no declared workspace on either endpoint: permissive logic
    # must not treat these as internal and under-report the external touch.
    target = NodeRef(
        node_id="n_child", path="/obj/ws/geo1", expected_type="geo", expected_workspace_id=None
    )
    source = NodeRef(
        node_id="n_ext", path="/obj/external", expected_type="geo", expected_workspace_id=None
    )
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        permission=PermissionMode.PROJECT_CHANGE,
        workspace_id=None,
        touches_external=False,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_project_change_denies_owned_id_with_external_path_underreported() -> None:
    target = _noderef()
    # source reuses an owned node_id but points at an external path
    source = _noderef(node_id="n_root", path="/obj/external", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=False,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_project_change_allows_external_wire_source_with_correct_flag() -> None:
    target = _noderef()
    source = NodeRef(
        node_id="n_ext", path="/obj/external", expected_type="geo", expected_workspace_id=None
    )
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, source),
        affected_paths=("/obj/ws/geo1", "/obj/external"),
        permission=PermissionMode.PROJECT_CHANGE,
        touches_external=True,
    )
    decision = evaluate_policy(cs, workspace=None)
    assert decision.allowed is True
    assert decision.denial_codes == ()


# --------------------------------------------------------------------------
# F6: an affected node that shares the target's identity but disagrees on
# path/type/workspace must not satisfy affected-node coverage.
# --------------------------------------------------------------------------


def test_owned_denies_affected_node_wrong_path_for_parm_target() -> None:
    target = _noderef()
    bad = _noderef(node_id="n_child", path="/obj/ws/WRONG")
    cs = _changeset((_setparm(target),), affected=(bad,), affected_paths=("/obj/ws/geo1",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_type_for_parm_target() -> None:
    target = _noderef()
    bad = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_type="WRONG")
    cs = _changeset((_setparm(target),), affected=(bad,), affected_paths=("/obj/ws/geo1",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_workspace_for_parm_target() -> None:
    target = _noderef()
    bad = _noderef(node_id="n_child", path="/obj/ws/geo1", expected_workspace_id=WS2)
    cs = _changeset((_setparm(target),), affected=(bad,), affected_paths=("/obj/ws/geo1",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_path_for_wire_target() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    bad_target = _noderef(node_id="n_child", path="/obj/ws/WRONG")
    cs = _changeset(
        (_wire(target, source),),
        affected=(bad_target, source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_path_for_wire_source() -> None:
    target = _noderef()
    source = _noderef(node_id="n_root", path="/obj/ws", expected_type="geo")
    bad_source = _noderef(node_id="n_root", path="/obj/ws/WRONG", expected_type="geo")
    cs = _changeset(
        (_wire(target, source),),
        affected=(target, bad_source),
        affected_paths=("/obj/ws/geo1", "/obj/ws"),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_path_for_created_node() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    bad = _noderef(node_id="n_new", path="/obj/ws/WRONG")  # derived path is /obj/ws/geo_new
    cs = _changeset((create,), affected=(bad,), affected_paths=("/obj/ws/geo_new",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


def test_owned_denies_affected_node_wrong_type_for_created_node() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    bad = _noderef(node_id="n_new", path="/obj/ws/geo_new", expected_type="WRONG")
    cs = _changeset((create,), affected=(bad,), affected_paths=("/obj/ws/geo_new",))
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


@pytest.mark.parametrize(
    "permission",
    [PermissionMode.OWNED_WORKSPACE, PermissionMode.SCOPED_PATCH, PermissionMode.PROJECT_CHANGE],
)
def test_affected_node_wrong_path_denied_across_modes(permission: PermissionMode) -> None:
    target = _noderef()  # /obj/ws/geo1
    bad = _noderef(node_id="n_child", path="/obj/ws/WRONG")
    scoped = ("n_child",) if permission is PermissionMode.SCOPED_PATCH else ()
    workspace = None if permission is PermissionMode.PROJECT_CHANGE else _manifest()
    touches_external = permission is PermissionMode.PROJECT_CHANGE
    cs = _changeset(
        (_setparm(target),),
        affected=(bad,),
        affected_paths=("/obj/ws/geo1",),
        permission=permission,
        scoped_node_ids=scoped,
        touches_external=touches_external,
    )
    decision = evaluate_policy(cs, workspace=workspace)
    assert decision.allowed is False
    assert "policy.affected_target_omitted" in decision.denial_codes


# --------------------------------------------------------------------------
# F7: duplicate created node ids / derived paths and manifest id reuse.
# --------------------------------------------------------------------------


def test_owned_denies_create_reusing_manifest_node_id() -> None:
    # n_child is already owned by the manifest; stable ids are not reusable.
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_child",
        node_type="geo",
        node_name="newchild",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_child", path="/obj/ws/newchild"),),
        affected_paths=("/obj/ws/newchild",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.node_id_reused" in decision.denial_codes


def test_owned_allows_create_with_new_node_id() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_new", path="/obj/ws/new"),),
        affected_paths=("/obj/ws/new",),
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


def test_conflicting_parent_reusing_created_id_is_external() -> None:
    # create n_a at /obj/ws/a, then create n_b whose parent claims n_a at a
    # conflicting path. That parent must count as external, not internal via
    # the reused created id.
    create_a = CreateNode(
        op_id="c_a",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_a",
        node_type="geo",
        node_name="a",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    create_b = CreateNode(
        op_id="c_b",
        parent=NodeRef(node_id="n_a", path="/obj/ws/WRONG", expected_type="geo", expected_workspace_id=WS),
        node_id="n_b",
        node_type="geo",
        node_name="b",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create_a, create_b),
        affected=(
            NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS),
            NodeRef(node_id="n_b", path="/obj/ws/WRONG/b", expected_type="geo", expected_workspace_id=WS),
        ),
        affected_paths=("/obj/ws/a", "/obj/ws/WRONG/b"),
        touches_external=False,  # the conflicting parent is external -> under-reported
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.effect_contradiction" in decision.denial_codes


def test_parent_matching_created_node_is_internal() -> None:
    # create n_a, then create n_b under n_a with the parent matching the
    # created n_a exactly -> the parent is internal, no external touch.
    create_a = CreateNode(
        op_id="c_a",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_a",
        node_type="geo",
        node_name="a",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    create_b = CreateNode(
        op_id="c_b",
        parent=NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS),
        node_id="n_b",
        node_type="geo",
        node_name="b",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create_a, create_b),
        affected=(
            NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS),
            NodeRef(node_id="n_b", path="/obj/ws/a/b", expected_type="geo", expected_workspace_id=WS),
        ),
        affected_paths=("/obj/ws/a", "/obj/ws/a/b"),
        touches_external=False,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()


# --------------------------------------------------------------------------
# F8: stable node id uniqueness is not a permission-mode privilege. When a
# manifest is supplied, any CreateNode reusing a manifest id is rejected
# regardless of the requested permission mode.
# --------------------------------------------------------------------------


def test_project_change_denies_create_reusing_manifest_node_id() -> None:
    # n_child is already owned by the supplied manifest; switching to
    # ProjectChange must not make the id reusable.
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_child",
        node_type="geo",
        node_name="new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_child", path="/obj/ws/new"),),
        affected_paths=("/obj/ws/new",),
        permission=PermissionMode.PROJECT_CHANGE,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is False
    assert "policy.node_id_reused" in decision.denial_codes


def test_project_change_allows_create_with_new_node_id() -> None:
    create = CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="geo"),
        node_id="n_new",
        node_type="geo",
        node_name="new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )
    cs = _changeset(
        (create,),
        affected=(_noderef(node_id="n_new", path="/obj/ws/new"),),
        affected_paths=("/obj/ws/new",),
        permission=PermissionMode.PROJECT_CHANGE,
    )
    decision = evaluate_policy(cs, workspace=_manifest())
    assert decision.allowed is True
    assert decision.denial_codes == ()
