"""Disposable Houdini 21 bootstrap smoke; run with hython, never pytest."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
for path in (ROOT, SITE_PACKAGES):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _fail(message: str) -> None:
    raise RuntimeError(f"BOOTSTRAP SMOKE FAIL: {message}")


def main() -> None:
    try:
        import hou
    except ImportError as exc:  # pragma: no cover - hython-only command
        raise RuntimeError("run this smoke with Houdini hython") from exc

    from eee_agent.changesets.contracts import ChangeSet, SetParm
    from eee_agent.houdini_bridge.changesets import ApplyRequest
    from eee_agent.modeling.bootstrap import derive_bootstrap_manifest
    from eee_agent.modeling.catalog import (
        houdini_21_minimal_catalog,
        houdini_21_minimal_quality_profile,
    )
    from eee_agent.modeling.compiler import (
        WorkspaceBootstrapContext,
        compile_bootstrap_procedural_spec,
    )
    from eee_agent.modeling.validation import (
        ValidationStatus,
        validate_applied_scene,
        validate_compilation,
        validate_parameter_sensitivity,
    )
    from eee_agent.modeling.contracts import (
        Axis,
        ComponentSpec,
        FrontAxis,
        InputBinding,
        ModelingBrief,
        NodeSpec,
        ParmAssignment,
        ProceduralSpec,
        UnitSystem,
    )
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    session_id = f"ses_{'a' * 32}"
    run_id = f"run_{'b' * 32}"
    workspace_id = f"ws_{'c' * 32}"
    root_name = "eee_bootstrap_smoke"
    rollback_root_name = "eee_bootstrap_rollback"
    now = datetime.now(timezone.utc)

    brief = ModelingBrief(
        brief_key="bootstrap_smoke",
        title="Bootstrap smoke",
        asset_family="test_asset",
        goal="Create and cook a box xform null chain.",
        units=UnitSystem.CENTIMETERS,
        up_axis=Axis.Y,
        front_axis=FrontAxis.NEGATIVE_Z,
        constraints=(),
    )
    components = (
        ComponentSpec(
            component_id="source",
            role="generator",
            depends_on=(),
            nodes=(
                NodeSpec(
                    node_key="box",
                    node_type="box",
                    node_name="box1",
                    parent_node=None,
                    parameters=(
                        ParmAssignment("sizex", 2.0),
                        ParmAssignment("sizey", 3.0),
                        ParmAssignment("sizez", 4.0),
                    ),
                    inputs=(),
                ),
            ),
        ),
        ComponentSpec(
            component_id="finish",
            role="transform",
            depends_on=("source",),
            nodes=(
                NodeSpec(
                    node_key="xform",
                    node_type="xform",
                    node_name="xform1",
                    parent_node=None,
                    parameters=(ParmAssignment("tx", 1.0),),
                    inputs=(InputBinding(0, "source.box", 0),),
                ),
                NodeSpec(
                    node_key="out",
                    node_type="null",
                    node_name="OUT_MODEL",
                    parent_node=None,
                    parameters=(),
                    inputs=(InputBinding(0, "finish.xform", 0),),
                ),
            ),
        ),
    )
    spec = ProceduralSpec(
        spec_key="bootstrap_smoke_v1",
        brief_digest=brief.digest,
        quality_profile_id="houdini_21_minimal_v1",
        workspace_root_node_id="bootstrap_root",
        components=components,
    )

    for path in (
        f"/obj/{root_name}",
        f"/obj/{rollback_root_name}",
    ):
        existing = hou.node(path)
        if existing is not None:
            existing.destroy()

    adapter = create_houdini_scene_adapter()
    root = None
    try:
        binding = adapter.binding()
        compiled = compile_bootstrap_procedural_spec(
            brief=brief,
            spec=spec,
            quality_profile=houdini_21_minimal_quality_profile(),
            catalog=houdini_21_minimal_catalog(),
            bootstrap=WorkspaceBootstrapContext(
                workspace_id=workspace_id,
                root_name=root_name,
            ),
            scene_binding=binding,
            session_id=session_id,
            run_id=run_id,
            change_id=f"chg_{'d' * 32}",
            created_at=now,
        )
        validation = validate_compilation(
            brief=brief,
            spec=spec,
            quality_profile=houdini_21_minimal_quality_profile(),
            catalog=houdini_21_minimal_catalog(),
            compilation=compiled,
        )
        graph_result = next(
            item for item in validation.results if item.validator.value == "Graph"
        )
        if graph_result.status is not ValidationStatus.PASSED:
            _fail("deterministic graph validation did not pass")
        executor = ChangeSetExecutor(adapter)
        request = ApplyRequest.build(
            request_id="bootstrap_smoke_apply",
            deadline_ms=30_000,
            scene_epoch=binding.scene_epoch,
            changeset=compiled.changeset,
            workspace=None,
        )
        receipt = executor.apply(request)
        if receipt.status.value != "Applied":
            _fail(f"expected Applied, got {receipt.status.value}")

        root = hou.node(f"/obj/{root_name}")
        box = hou.node(f"/obj/{root_name}/box1")
        xform = hou.node(f"/obj/{root_name}/xform1")
        output = hou.node(f"/obj/{root_name}/OUT_MODEL")
        if any(node is None for node in (root, box, xform, output)):
            _fail("the compiled graph is incomplete")
        if box.parm("sizex").eval() != 2.0 or xform.parm("tx").eval() != 1.0:
            _fail("cataloged parameters were not applied")
        if xform.inputs()[0] != box or output.inputs()[0] != xform:
            _fail("the graph wiring does not match the strict spec")
        geometry = output.geometry()
        if geometry is None or len(geometry.prims()) == 0:
            _fail("the output did not cook non-empty geometry")
        scene_query = adapter.scene_query(
            include_selection=False,
            node_paths=[node.path for node in compiled.changeset.affected_nodes],
            include_geometry_stats=True,
            expected_scene_epoch=binding.scene_epoch,
        )
        post_apply = validate_applied_scene(
            changeset=compiled.changeset,
            query=scene_query,
        )
        if any(item.status is not ValidationStatus.PASSED for item in post_apply):
            _fail(
                "typed post-Apply Cook/Geometry validation did not pass: "
                f"{[item.to_dict() for item in post_apply]}"
            )
        baseline_query = scene_query
        box.parm("sizex").set(3.0)
        sample_query = adapter.scene_query(
            include_selection=False,
            node_paths=[node.path for node in compiled.changeset.affected_nodes],
            include_geometry_stats=True,
            expected_scene_epoch=binding.scene_epoch,
        )
        box.parm("sizex").set(2.0)
        restored_query = adapter.scene_query(
            include_selection=False,
            node_paths=[node.path for node in compiled.changeset.affected_nodes],
            include_geometry_stats=True,
            expected_scene_epoch=binding.scene_epoch,
        )
        sensitivity = validate_parameter_sensitivity(
            changeset=compiled.changeset,
            baseline=baseline_query,
            samples=(sample_query,),
            restored=restored_query,
        )
        if sensitivity.status is not ValidationStatus.PASSED:
            _fail(f"parameter sensitivity did not restore: {sensitivity.to_dict()}")

        for node in (root, box, xform, output):
            expected = {
                "eee.workspace_id": workspace_id,
                "eee.capability": "modeling",
                "eee.schema_version": "1",
                "eee.created_by_run": run_id,
            }
            for key, value in expected.items():
                if node.userData(key) != value:
                    _fail(f"{node.path()} has invalid {key}")
            if not node.userData("eee.node_id") or not node.userData("eee.role"):
                _fail(f"{node.path()} is missing owned identity")

        manifest = derive_bootstrap_manifest(compiled.changeset, receipt)
        if manifest.workspace_id != workspace_id or len(manifest.nodes) != 4:
            _fail("the applied receipt did not derive the exact Workspace")
        if manifest.roots[0].path != f"/obj/{root_name}":
            _fail("the derived Workspace root is wrong")

        replay = executor.apply(request)
        if replay.status.value != "AlreadyApplied":
            _fail(f"expected AlreadyApplied replay, got {replay.status.value}")

        rollback_compiled = compile_bootstrap_procedural_spec(
            brief=brief,
            spec=spec,
            quality_profile=houdini_21_minimal_quality_profile(),
            catalog=houdini_21_minimal_catalog(),
            bootstrap=WorkspaceBootstrapContext(
                workspace_id=f"ws_{'e' * 32}",
                root_name=rollback_root_name,
            ),
            scene_binding=adapter.binding(),
            session_id=session_id,
            run_id=run_id,
            change_id=f"chg_{'f' * 32}",
            created_at=now,
        )
        operations = list(rollback_compiled.changeset.operations)
        parm_index = next(
            index for index, operation in enumerate(operations)
            if isinstance(operation, SetParm)
        )
        operations[parm_index] = replace(
            operations[parm_index], expected_old_value=99.0
        )
        forced = replace(
            rollback_compiled.changeset,
            operations=tuple(operations),
        )
        forced_request = ApplyRequest.build(
            request_id="bootstrap_smoke_rollback",
            deadline_ms=30_000,
            scene_epoch=forced.scene_binding.scene_epoch,
            changeset=forced,
            workspace=None,
        )
        rolled_back = executor.apply(forced_request)
        if rolled_back.status.value != "RolledBack":
            _fail(f"expected RolledBack, got {rolled_back.status.value}")
        if hou.node(f"/obj/{rollback_root_name}") is not None:
            _fail("forced rollback left a bootstrap root behind")

        print(
            "BOOTSTRAP SMOKE PASS: Applied, Cook/Geometry, sensitivity restore, "
            "owned metadata, manifest, AlreadyApplied, and RolledBack cleanup"
        )
    finally:
        for path in (
            f"/obj/{root_name}",
            f"/obj/{rollback_root_name}",
        ):
            node = hou.node(path)
            if node is not None:
                node.destroy()
        adapter.close()


if __name__ == "__main__":
    main()
