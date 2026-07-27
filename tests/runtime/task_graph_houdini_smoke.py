"""Run the node-lifecycle executor surfaces under hython (not pytest)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / ".venv" / "Lib" / "site-packages"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    try:
        import hou
    except ImportError:
        die("hou is not importable; run with hython")
    from eee_agent.houdini_bridge.scratch import (
        ScratchCommitRequest,
        ScratchDeleteRequest,
        ScratchOp,
        ScratchRequest,
        ScratchTopologyRequest,
    )
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    adapter = create_houdini_scene_adapter()
    executor = ChangeSetExecutor(adapter)
    epoch = executor.binding().scene_epoch
    target = "/obj/eee_taskgraph_smoke"
    old = hou.node(target)
    if old is not None:
        old.destroy()
    try:
        built = executor.scratch_exec(
            ScratchRequest.build(
                request_id="smoke_build",
                deadline_ms=30000,
                scene_epoch=epoch,
                sandbox_id="taskgraph_smoke",
                operations=(
                    ScratchOp(kind="create_node", node_name="box1",
                              node_type="box", note="smoke box"),
                    ScratchOp(kind="create_node", node_name="xform1", node_type="xform"),
                    ScratchOp(kind="connect", node_name="xform1", input_index=0,
                              source="box1", source_output_index=0),
                ),
                purpose="smoke build",
            )
        )
        if built.applied_ops != 3:
            die(f"build applied_ops={built.applied_ops}")
        verdict = executor.scratch_commit(
            ScratchCommitRequest.build(
                request_id="smoke_commit",
                deadline_ms=30000,
                scene_epoch=epoch,
                sandbox_id="taskgraph_smoke",
                target_parent_path="/obj",
                target_name="eee_taskgraph_smoke",
                skip_structure_check=True,
                annotations={"box1": "smoke annotation"},
            )
        )
        if not verdict.committed:
            die(f"commit refused: {verdict.reason}")
        box, output = hou.node(f"{target}/box1"), hou.node(f"{target}/xform1")
        if box is None or output is None:
            die("committed nodes missing")
        if abs((float(output.position()[0]) - float(box.position()[0])) - 3.0) > 1e-6:
            die("layered layout not applied")
        if not output.isDisplayFlagSet() or not output.isRenderFlagSet():
            die(
                f"display/render flags missing display={output.isDisplayFlagSet()} "
                f"render={output.isRenderFlagSet()} type={output.type().name()} "
                f"warnings={verdict.warnings}"
            )
        if box.comment() != "smoke annotation":
            die("annotation missing")
        orphan = hou.node(target).createNode("null", "orphan1")
        topo = executor.scratch_topology(
            ScratchTopologyRequest.build(
                request_id="smoke_topo", deadline_ms=30000, scene_epoch=epoch,
                paths=(orphan.path(), output.path()),
            )
        )
        if topo.nodes[0]["outputs"]:
            die("orphan unexpectedly has outputs")
        # Regression: inputs must come from node.inputs(); on real Houdini
        # 21.0.440 inputConnections().outputNode() returns the node itself.
        if topo.nodes[1]["inputs"] != [box.path()]:
            die(f"xform topology inputs wrong: {topo.nodes[1]['inputs']}")
        orphan_path = orphan.path()
        deleted = executor.delete_nodes(
            ScratchDeleteRequest.build(
                request_id="smoke_delete", deadline_ms=30000, scene_epoch=epoch,
                allowed_paths=(orphan_path,), paths=(orphan_path,),
            )
        )
        if deleted.deleted_paths != (orphan_path,):
            die(f"delete verdict={deleted}")
        print("SMOKE OK: node lifecycle (finalize + topology + delete)")
    finally:
        node = hou.node(target)
        if node is not None:
            node.destroy()


if __name__ == "__main__":
    if "pytest" in sys.argv[0].lower():
        die("this smoke must run with hython")
    main()
