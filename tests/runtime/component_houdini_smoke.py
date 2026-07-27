"""Deterministic Wave C component/subnet smoke for Houdini hython.

The smoke exercises the production scratch executor directly (no Runtime,
Bridge server, Provider, or LLM).  It builds two SOP subnets with public spare
parameters, typed derived expressions, a single top-level merge/OUT sink, commits a
Parameter Manifest into the container comment, and then deletes nested
targets in dependency-safe order.
"""
from __future__ import annotations

import json
import os
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / ".venv" / "Lib" / "site-packages"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


def die(message: str) -> None:
    print(f"COMPONENT SMOKE FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hip-out",
        default=None,
        help="optional HIP path to save after commit and before cleanup",
    )
    args = parser.parse_args()
    try:
        import hou
        from eee_agent.houdini_bridge.scratch import (
            ScratchCommitRequest,
            ScratchExpr,
            ScratchOp,
            ScratchParmDeclaration,
            ScratchRequest,
            ScratchDeleteRequest,
        )
        from houdini_side.changeset_executor import ChangeSetExecutor
        from houdini_side.secure_bridge import create_houdini_scene_adapter
    except ImportError as exc:  # pragma: no cover - hython-only
        raise RuntimeError("run this smoke with Houdini hython") from exc

    token = f"{os.getpid()}_{int(time.time())}"
    sandbox_id = f"component_{token}"
    sandbox_root = f"/obj/eee_scratch_{sandbox_id}"
    final_path = f"/obj/eee_component_{token}"
    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    executor = ChangeSetExecutor(adapter)
    epoch = int(adapter.binding().scene_epoch)

    def request(ops: tuple[ScratchOp, ...]) -> ScratchRequest:
        return ScratchRequest.build(
            request_id=f"component_build_{token}",
            deadline_ms=30_000,
            scene_epoch=epoch,
            sandbox_id=sandbox_id,
            operations=ops,
            purpose="Wave C deterministic nested component smoke",
        )

    manifest = (
        ScratchParmDeclaration(
            name="wheel_width",
            tab="Wheel",
            binding=(("node", "wheel"), ("parm", "wheel_width")),
            default=1.0,
            min=0.5,
            max=1.5,
            unit="m",
        ),
        ScratchParmDeclaration(
            name="frame_width",
            tab="Frame",
            binding=(("node", "frame"), ("parm", "frame_width")),
            default=2.0,
            min=1.0,
            max=3.0,
            unit="m",
        ),
        ScratchParmDeclaration(
            name="frame_derived_width",
            tab="Frame",
            classification="derived",
            binding=(("node", "frame/frame_body"), ("parm", "sizex")),
            default=4.0,
            depends_on=("frame_width",),
            unit="m",
        ),
    )

    old = hou.node(final_path)
    if old is not None:
        old.destroy()
    old = hou.node(sandbox_root)
    if old is not None:
        old.destroy()

    try:
        built = executor.scratch_exec(
            request(
                (
                    ScratchOp(kind="create_node", node_name="wheel", node_type="subnet"),
                    ScratchOp(
                        kind="declare_parm",
                        node_name="wheel",
                        parm="wheel_width",
                        label="Wheel width",
                        value=1.0,
                        minimum=0.5,
                        maximum=1.5,
                        unit="m",
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="wheel_body",
                        node_type="box",
                        parent="wheel",
                    ),
                    ScratchOp(
                        kind="set_parm",
                        node_name="wheel/wheel_body",
                        parm="sizex",
                        expr=ScratchExpr(
                            kind="ref", path="../wheel_width"
                        ),
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="wheel_output",
                        node_type="output",
                        parent="wheel",
                    ),
                    ScratchOp(
                        kind="connect",
                        node_name="wheel/wheel_output",
                        input_index=0,
                        source="wheel/wheel_body",
                        source_output_index=0,
                    ),
                    ScratchOp(kind="create_node", node_name="frame", node_type="subnet"),
                    ScratchOp(
                        kind="declare_parm",
                        node_name="frame",
                        parm="frame_width",
                        label="Frame width",
                        value=2.0,
                        minimum=1.0,
                        maximum=3.0,
                        unit="m",
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="frame_body",
                        node_type="box",
                        parent="frame",
                    ),
                    ScratchOp(
                        kind="set_parm",
                        node_name="frame/frame_body",
                        parm="sizex",
                        expr=ScratchExpr(
                            kind="op",
                            name="mul",
                            args=(
                                ScratchExpr(
                                    kind="ref",
                                    path="../frame_width",
                                ),
                                ScratchExpr(kind="num", value=2.0),
                            ),
                        ),
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="frame_output",
                        node_type="output",
                        parent="frame",
                    ),
                    ScratchOp(
                        kind="connect",
                        node_name="frame/frame_output",
                        input_index=0,
                        source="frame/frame_body",
                        source_output_index=0,
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="assembled",
                        node_type="merge",
                    ),
                    ScratchOp(
                        kind="connect",
                        node_name="assembled",
                        input_index=0,
                        source="wheel",
                        source_output_index=0,
                    ),
                    ScratchOp(
                        kind="connect",
                        node_name="assembled",
                        input_index=1,
                        source="frame",
                        source_output_index=0,
                    ),
                    ScratchOp(
                        kind="create_node",
                        node_name="OUT",
                        node_type="null",
                    ),
                    ScratchOp(
                        kind="connect",
                        node_name="OUT",
                        input_index=0,
                        source="assembled",
                        source_output_index=0,
                    ),
                )
            )
        )
        expect(built.applied_ops == 17, f"applied_ops={built.applied_ops}")
        expect(not built.errors, f"cook errors={built.errors}")
        expect(
            built.output_node.endswith("/OUT"),
            f"output_node={built.output_node}",
        )

        verdict = executor.scratch_commit(
            ScratchCommitRequest.build(
                request_id=f"component_commit_{token}",
                deadline_ms=30_000,
                scene_epoch=epoch,
                sandbox_id=sandbox_id,
                target_parent_path="/obj",
                target_name=f"eee_component_{token}",
                skip_structure_check=True,
                annotations={
                    "wheel": "Wheel component",
                    "frame": "Frame component",
                },
                parameters=manifest,
            )
        )
        expect(verdict.committed, f"commit refused={verdict.reason}")
        expect(verdict.receipt.get("parameter_tabs") == {
            "Wheel": ["wheel_width"],
            "Frame": ["frame_width", "frame_derived_width"],
        }, f"parameter_tabs={verdict.receipt.get('parameter_tabs')}")
        expect("parameter_ranges" in verdict.receipt, "ranges missing from receipt")

        container = hou.node(final_path)
        expect(container is not None, "committed container missing")
        stored = json.loads(container.comment())
        expect(len(stored) == 3, f"manifest comment entries={len(stored)}")
        expect(hou.node(f"{final_path}/wheel").comment() == "Wheel component",
               "nested wheel annotation missing")
        expect(container.node("wheel").parm("wheel_width") is not None,
               "public wheel parameter missing")
        expect(container.node("frame").parm("frame_width") is not None,
               "public frame parameter missing")
        expect(
            not any(child.name().endswith("_ctrl")
                    for child in container.allSubChildren()),
            "internal ctrl node leaked into committed component",
        )
        wheel = container.node("wheel")
        frame = container.node("frame")
        wheel.parm("wheel_width").set(1.5)
        frame.parm("frame_width").set(3.0)
        output = hou.node(f"{final_path}/OUT")
        expect(output is not None, "OUT node missing")
        output.cook(force=True)
        expect(container.node("wheel/wheel_body").parm("sizex").eval() == 1.5,
               "wheel public parameter did not drive body")
        expect(container.node("frame/frame_body").parm("sizex").eval() == 6.0,
               "frame public parameter did not drive derived body")
        wheel.parm("wheel_width").set(1.0)
        frame.parm("frame_width").set(2.0)
        expect(output is not None and output.isDisplayFlagSet(), "OUT display flag missing")
        expect(output.isRenderFlagSet(), "OUT render flag missing")
        if args.hip_out:
            hou.hipFile.save(args.hip_out)

        # The explicit delete set includes all descendants and their consumers;
        # the executor accepts the deep-first order and leaves no committed
        # component node behind.
        paths = (
            f"{final_path}/wheel/wheel_body",
            f"{final_path}/wheel/wheel_output",
            f"{final_path}/frame/frame_body",
            f"{final_path}/frame/frame_output",
            f"{final_path}/wheel",
            f"{final_path}/frame",
            f"{final_path}/assembled",
            f"{final_path}/OUT",
            final_path,
        )
        deleted = executor.delete_nodes(
            ScratchDeleteRequest.build(
                request_id=f"component_delete_{token}",
                deadline_ms=30_000,
                scene_epoch=epoch,
                allowed_paths=paths,
                paths=paths,
            )
        )
        expect(len(deleted.deleted_paths) == len(paths), f"deleted={deleted}")
        expect(hou.node(final_path) is None, "committed component still exists")
        print("COMPONENT SMOKE OK")
        return 0
    finally:
        for path in (final_path, sandbox_root):
            node = hou.node(path)
            if node is not None:
                with hou.undos.disabler():
                    node.destroy()


if __name__ == "__main__":
    if "pytest" in sys.argv[0].lower():
        die("this smoke must run with hython")
    raise SystemExit(main())
