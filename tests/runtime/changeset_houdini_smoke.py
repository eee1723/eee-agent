"""Task 16-D disposable real-Houdini smoke (run with hython, NOT pytest).

Exercises one minimal create + set + connect transaction through the real
:class:`ChangeSetExecutor` against the live ``hou`` module, then a receipt
query and an idempotent replay (``AlreadyApplied``). It creates only a private
``/obj/eee_task16d_smoke`` branch, destroys it on exit, and never saves, loads,
clears, or exports the user's HIP.

Run (detected hython)::

    "<Houdini>/bin/hython.exe" tests/runtime/changeset_houdini_smoke.py

Exit code 0 on success; non-zero with a message on any failure. The module is
imported by hython only (``hou`` is unavailable under pytest/the venv), so it
guards on ``hou`` and is excluded from offline collection by name/convention.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    try:
        import hou  # noqa: F401  — only available inside hython
    except ImportError:
        _die("hou is not importable; run this script with hython, not pytest/python.")

    from eee_agent.changesets import (
        ChangeSet,
        CheckpointPlan,
        ConnectInput,
        CreateNode,
        NodeIdentityEquals,
        NodeRef,
        OwnedNodeRef,
        ParmValueEquals,
        PermissionMode,
        RiskSummary,
        SetParm,
        WireInputEquals,
        WireRef,
        WorkspaceManifest,
    )
    from eee_agent.houdini_bridge.changesets import ApplyRequest
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    SES = f"ses_{'0' * 32}"
    RUN = f"run_{'1' * 32}"
    WS = f"ws_{'a' * 32}"
    ROOT_PATH = "/obj/eee_task16d_smoke"
    CHILD_PATH = ROOT_PATH + "/geo1"  # geometry object (SOP container)
    BOX_PATH = CHILD_PATH + "/box"  # pre-existing SOP (wire source)
    XFORM0_PATH = CHILD_PATH + "/xform0"  # pre-existing SOP (parm + wire target)
    NULL_PATH = CHILD_PATH + "/null1"  # SOP created by the transaction
    NOW = datetime.now(timezone.utc)

    def mirror(node, node_id: str, role: str) -> None:
        node.setUserData("eee.workspace_id", WS)
        node.setUserData("eee.node_id", node_id)
        node.setUserData("eee.capability", "modeling")
        node.setUserData("eee.role", role)
        node.setUserData("eee.schema_version", "1")
        node.setUserData("eee.created_by_run", RUN)

    adapter = create_houdini_scene_adapter()
    binding = adapter.binding()

    # Fresh disposable namespace (destroy any prior smoke branch, then rebuild).
    pre = hou.node(ROOT_PATH)
    if pre is not None:
        pre.destroy()
    try:
        root = hou.node("/obj").createNode("subnet", "eee_task16d_smoke")
        child = root.createNode("geo", "geo1")
        box = child.createNode("box", "box")  # pre-existing SOP wire source
        xform0 = child.createNode("xform", "xform0")  # pre-existing SOP (parm + wire target)
        xform0.parm("tx").set(0.0)
        mirror(root, "n_root", "root")
        mirror(child, "n_child", "member")
        mirror(box, "n_box", "member")
        mirror(xform0, "n_xform0", "member")

        owned_root = OwnedNodeRef(
            node_id="n_root", path=ROOT_PATH, node_type="subnet",
            parent_path="/obj", capability="modeling", role="root",
        )
        owned_child = OwnedNodeRef(
            node_id="n_child", path=CHILD_PATH, node_type="geo",
            parent_path=ROOT_PATH, capability="modeling", role="member",
        )
        owned_box = OwnedNodeRef(
            node_id="n_box", path=BOX_PATH, node_type="box",
            parent_path=CHILD_PATH, capability="modeling", role="member",
        )
        owned_xform0 = OwnedNodeRef(
            node_id="n_xform0", path=XFORM0_PATH, node_type="xform",
            parent_path=CHILD_PATH, capability="modeling", role="member",
        )
        manifest = WorkspaceManifest.build(
            workspace_id=WS, session_id=SES, instance_id=binding.instance_id,
            scene_epoch=binding.scene_epoch, roots=[owned_root],
            nodes=[owned_root, owned_child, owned_box, owned_xform0],
            created_by_run=RUN, updated_at=NOW,
        )

        child_ref = NodeRef(
            node_id="n_child", path=CHILD_PATH, expected_type="geo", expected_workspace_id=WS,
        )
        box_ref = NodeRef(
            node_id="n_box", path=BOX_PATH, expected_type="box", expected_workspace_id=WS,
        )
        xform0_ref = NodeRef(
            node_id="n_xform0", path=XFORM0_PATH, expected_type="xform", expected_workspace_id=WS,
        )
        null_ref = NodeRef(
            node_id="n_new", path=NULL_PATH, expected_type="null", expected_workspace_id=WS,
        )
        # connect targets a PRE-EXISTING node (xform0) so the accepted policy
        # engine (which cannot see transaction-created nodes as owned) accepts it.
        create = CreateNode(
            op_id="op_c", parent=child_ref, node_id="n_new", node_type="null", node_name="null1",
            workspace_id=WS, capability="modeling", role="member",
        )
        setparm = SetParm(
            op_id="op_s", target=xform0_ref, parm_name="tx", value=5.0, expected_old_value=0.0,
        )
        connect = ConnectInput(
            op_id="op_w", target=xform0_ref, input_index=0, source=box_ref,
            source_output_index=0, expected_old_source=None,
        )
        ops = (create, setparm, connect)
        # Derive every mandatory pre/postcondition + checkpoint fact so the
        # executor's F2 enforcement accepts the transaction.
        from houdini_side.changeset_executor import (  # noqa: E402
            _dedup,
            derive_mandatory_checkpoint,
            derive_mandatory_postconditions,
            derive_mandatory_preconditions,
        )
        pre = tuple(_dedup(derive_mandatory_preconditions(ops, binding, manifest)))
        post = tuple(_dedup(derive_mandatory_postconditions(ops)))
        cnodes, cparms, cwires = derive_mandatory_checkpoint(ops)
        changeset = ChangeSet(
            change_id=f"chg_{'5' * 32}", session_id=SES, run_id=RUN, scene_binding=binding,
            workspace_id=WS, base_revision="a" * 64,
            required_permission=PermissionMode.OWNED_WORKSPACE, scoped_node_ids=(),
            operations=ops,
            affected_nodes=(null_ref, xform0_ref, box_ref), read_dependencies=(),
            preconditions=pre, expected_postconditions=post,
            risk_summary=RiskSummary(
                touches_external_nodes=False, changes_wiring=True, requires_backup=False,
                operation_count=3, effect_names=("node.create", "parm.set", "wire.connect"),
                affected_paths=(NULL_PATH, XFORM0_PATH, BOX_PATH),
            ),
            checkpoint_plan=CheckpointPlan(
                nodes=tuple(_dedup(cnodes)), parameters=tuple(_dedup(cparms)), wires=tuple(_dedup(cwires)),
            ),
            created_at=NOW,
        )
        request = ApplyRequest.build(
            request_id="req_smoke", deadline_ms=30000, scene_epoch=binding.scene_epoch,
            changeset=changeset, workspace=manifest,
        )

        executor = ChangeSetExecutor(adapter)
        receipt = executor.apply(request)
        print(f"apply status={receipt.status.value} ops={receipt.applied_op_ids} "
              f"scene_may_have_changed={receipt.scene_may_have_changed}")
        if receipt.status.value != "Applied":
            _die(
                f"expected Applied, got {receipt.status.value}; "
                f"post={receipt.postcondition_results}; rollback={receipt.rollback_results}"
            )
        if receipt.applied_op_ids != ("op_c", "op_s", "op_w"):
            _die(f"unexpected applied op order: {receipt.applied_op_ids}")

        # verify the real scene reflects the writes
        if hou.node(NULL_PATH) is None:
            _die("created null SOP not present after apply")
        if xform0.parm("tx").eval() != 5.0:
            _die("xform0 parm tx not set to 5.0 after apply")
        wired = xform0.inputs()[0] if xform0.inputs() else None
        if wired is None or wired.path() != BOX_PATH:
            _die("xform0 input 0 not wired to the box SOP after apply")

        # receipt query returns the cached terminal receipt (no scene mutation)
        cached = executor.receipt(changeset.change_id, changeset.digest)
        if cached is None or cached.status.value != "Applied":
            _die("receipt query did not return the cached Applied receipt")

        # idempotent replay performs zero writes and returns AlreadyApplied
        replay = executor.apply(request)
        print(f"replay status={replay.status.value} ops={replay.applied_op_ids}")
        if replay.status.value != "AlreadyApplied":
            _die(f"expected AlreadyApplied on replay, got {replay.status.value}")

        print("SMOKE OK: create/set/connect applied, receipt cached, replay idempotent")
    finally:
        node = hou.node(ROOT_PATH)
        if node is not None:
            node.destroy()
            print(f"cleanup: destroyed {ROOT_PATH}")


if __name__ == "__main__":
    if "PYTEST" in "".join(sys.argv).upper() or "pytest" in sys.argv[0]:
        _die("this smoke runs under hython, not pytest")
    main()
