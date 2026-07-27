"""Real-Houdini smoke for scratch.v2 C1 typed parameter expressions.

Run with Houdini's ``hython``; this is not a pytest module.  The smoke uses
the real ``hou`` module and the production ``ChangeSetExecutor`` directly.  It
builds two boxes in a run-scoped sandbox: ``box1`` with a literal ``sizex``
and ``box2`` whose ``sizex`` is a typed expression referencing
``../box1/sizex * 2``.  It verifies the rendered channel reference evaluates
on cook and tracks the referenced parm when the source changes, then checks
the negative paths (sandbox-escaping refs, non-whitelisted functions) fail
closed.  Every node it creates is removed in ``finally``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]
_SITE_PACKAGES = _ROOT / ".venv" / "Lib" / "site-packages"
for _path in (_ROOT, _SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _die(message: str) -> None:
    raise RuntimeError(f"EXPR SMOKE FAIL: {message}")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        _die(message)


def main() -> int:
    try:
        import hou

        from eee_agent.houdini_bridge.scratch import (
            ScratchExpr,
            ScratchOp,
            ScratchRequest,
        )
        from houdini_side.changeset_executor import ChangeSetExecutor
        from houdini_side.secure_bridge import create_houdini_scene_adapter
    except ImportError as exc:  # pragma: no cover - hython-only command
        raise RuntimeError("run this smoke with Houdini hython") from exc

    token = f"{os.getpid()}_{int(time.time())}"
    sandbox_id = f"expr_{token}"
    container_path = f"/obj/eee_scratch_{sandbox_id}"

    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    executor = ChangeSetExecutor(adapter)

    def scratch_request(operations: tuple[ScratchOp, ...]) -> ScratchRequest:
        return ScratchRequest.build(
            request_id=f"expr_smoke_{token}",
            deadline_ms=30_000,
            scene_epoch=int(adapter.binding().scene_epoch),
            sandbox_id=sandbox_id,
            operations=operations,
            purpose="real Houdini expression smoke functional unit",
        )

    def box_x_size(name: str) -> float:
        node = hou.node(f"{container_path}/{name}")
        _expect(node is not None, f"{name} is missing")
        node.cook(force=True)
        geometry = node.geometry()
        bbox = geometry.boundingBox()
        return float(bbox.sizevec().x())

    try:
        _expect(hou.node("/obj") is not None, "/obj network is unavailable")

        # 1. box1 literal sizex=1.0; box2 sizex = ch(../box1/sizex) * 2.
        built = executor.scratch_exec(scratch_request((
            ScratchOp(kind="create_node", node_name="box1", node_type="box"),
            ScratchOp(kind="set_parm", node_name="box1", parm="sizex", value=1.0),
            ScratchOp(kind="create_node", node_name="box2", node_type="box"),
            ScratchOp(
                kind="set_parm", node_name="box2", parm="sizex",
                expr=ScratchExpr(kind="op", name="mul", args=(
                    ScratchExpr(kind="ref", path="../box1/sizex"),
                    ScratchExpr(kind="num", value=2.0),
                )),
            ),
        )))
        _expect(built.applied_ops == 4, f"build applied {built.applied_ops} ops")
        _expect(not built.errors, f"build cooked with errors: {built.errors}")

        # 2. The expression evaluates: box2 x size == 2.0.
        size = box_x_size("box2")
        _expect(abs(size - 2.0) <= 1e-4, f"box2 x size {size} != 2.0")

        # 3. Tracking: change box1 sizex to 1.5; box2 must follow to 3.0.
        executor.scratch_exec(scratch_request((
            ScratchOp(kind="set_parm", node_name="box1", parm="sizex", value=1.5),
        )))
        size = box_x_size("box2")
        _expect(abs(size - 3.0) <= 1e-4, f"box2 x size {size} != 3.0 after update")

        # 4a. Negative: a ref escaping the sandbox fails the op.
        for bad_ref in ("../../box1/sizex", "/obj/elsewhere/sizex"):
            try:
                executor.scratch_exec(scratch_request((
                    ScratchOp(
                        kind="set_parm", node_name="box2", parm="sizex",
                        expr=ScratchExpr(kind="ref", path=bad_ref),
                    ),
                )))
            except Exception as exc:
                _expect(
                    "sandbox" in str(exc).lower(),
                    f"escape error is not diagnostic: {exc}",
                )
            else:
                _die(f"escaping ref unexpectedly succeeded: {bad_ref}")

        # 4b. Negative: non-whitelisted functions are rejected by the DTO.
        for bad_func in ("python", "eval", "bbox"):
            try:
                ScratchExpr(
                    kind="func", name=bad_func,
                    args=(ScratchExpr(kind="num", value=1.0),),
                )
            except ValueError:
                pass
            else:
                _die(f"non-whitelisted func unexpectedly accepted: {bad_func}")

        print("EXPR SMOKE OK")
        return 0
    finally:
        node = hou.node(container_path)
        if node is not None:
            with hou.undos.disabler():
                node.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
