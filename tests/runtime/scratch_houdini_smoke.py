"""Real-Houdini smoke for the sandbox -> verify -> commit path.

Run with Houdini's ``hython``; this is not a pytest module.  The smoke uses the
real ``hou`` module and the production ``ChangeSetExecutor`` directly, without
Runtime, Secure Bridge, an LLM, or provider credentials.  It creates only
run-scoped nodes under ``/obj``, never saves/loads/clears/exports the user's HIP,
and removes every node it created in ``finally``.

The Bridge and Agent layers have separate smokes.  Keeping this script direct
is intentional: a failure here means Houdini/HOM/catalog/gates, not transport
or model behavior.
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
    raise RuntimeError(f"SCRATCH SMOKE FAIL: {message}")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        _die(message)


def _created_node_paths(hou, prefix: str) -> tuple[str, ...]:
    obj = hou.node("/obj")
    if obj is None:
        return ()
    return tuple(
        child.path()
        for child in obj.children()
        if child.name().startswith(prefix)
    )


def main() -> int:
    try:
        import hou

        from eee_agent.houdini_bridge.scratch import (
            ScratchCommitRequest,
            ScratchOp,
            ScratchRequest,
        )
        from houdini_side.changeset_executor import ChangeSetExecutor
        from houdini_side.secure_bridge import create_houdini_scene_adapter
    except ImportError as exc:  # pragma: no cover - hython-only command
        raise RuntimeError("run this smoke with Houdini hython") from exc

    token = f"{os.getpid()}_{int(time.time())}"
    sandbox_id = f"smoke_{token}"
    refused_id = f"refused_{token}"
    invalid_id = f"invalid_{token}"
    orientation_id = f"orient_{token}"
    target_name = f"eee_smoke_asset_{token}"
    created_prefixes = (
        f"eee_scratch_{sandbox_id}",
        f"eee_scratch_{refused_id}",
        f"eee_scratch_{invalid_id}",
        f"eee_scratch_{orientation_id}",
        target_name,
    )

    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    executor = ChangeSetExecutor(adapter)

    def request_id(label: str) -> str:
        return f"scratch_smoke_{label}_{token}"

    def scene_epoch() -> int:
        return int(adapter.binding().scene_epoch)

    def scratch_request(
        sid: str,
        operations: tuple[ScratchOp, ...],
        *,
        preserve_on_failure: bool = True,
    ) -> ScratchRequest:
        return ScratchRequest.build(
            request_id=request_id(sid),
            deadline_ms=30_000,
            scene_epoch=scene_epoch(),
            sandbox_id=sid,
            operations=operations,
            preserve_on_failure=preserve_on_failure,
        )

    def commit_request(
        sid: str,
        *,
        name: str,
        orientation_checks: tuple[dict[str, object], ...] = (),
    ) -> ScratchCommitRequest:
        return ScratchCommitRequest.build(
            request_id=request_id(f"commit_{sid}"),
            deadline_ms=30_000,
            scene_epoch=scene_epoch(),
            sandbox_id=sid,
            target_parent_path="/obj",
            target_name=name,
            orientation_checks=orientation_checks,
            skip_structure_check=True,
        )

    def destroy(sid: str) -> None:
        from eee_agent.houdini_bridge.scratch import ScratchDestroyRequest

        executor.scratch_destroy(
            ScratchDestroyRequest.build(
                request_id=request_id(f"destroy_{sid}"),
                deadline_ms=30_000,
                scene_epoch=scene_epoch(),
                sandbox_id=sid,
            )
        )

    try:
        _expect(hou.node("/obj") is not None, "/obj network is unavailable")
        _expect(
            hou.node(f"/obj/{target_name}") is None,
            "target path unexpectedly exists before smoke",
        )

        # 1. Valid catalog path: box -> set literal parms -> cook -> commit.
        valid_ops = (
            ScratchOp(kind="create_node", node_name="box1", node_type="box"),
            ScratchOp(kind="set_parm", node_name="box1", parm="sizex", value=2.0),
            ScratchOp(kind="set_parm", node_name="box1", parm="sizey", value=1.0),
            ScratchOp(kind="set_parm", node_name="box1", parm="sizez", value=3.0),
        )
        built = executor.scratch_exec(scratch_request(sandbox_id, valid_ops))
        _expect(built.applied_ops == 4, f"valid build applied {built.applied_ops} ops")
        _expect(not built.errors, f"valid build cooked with errors: {built.errors}")
        _expect(built.geometry is not None, "valid build returned no geometry")
        _expect(built.geometry.point_count > 0, "valid build has no points")
        _expect(built.geometry.prim_count > 0, "valid build has no primitives")
        committed = executor.scratch_commit(
            commit_request(sandbox_id, name=target_name)
        )
        _expect(committed.committed is True, f"valid commit refused: {committed.reason}")
        _expect(committed.refused is False, "valid commit marked refused")
        _expect(hou.node(f"/obj/{target_name}") is not None, "final node is missing")
        _expect(hou.node(f"/obj/eee_scratch_{sandbox_id}") is None, "sandbox survived commit")

        # 2. Invalid literal parm: operation fails honestly and preserves sandbox.
        invalid_ops = (
            ScratchOp(kind="create_node", node_name="box1", node_type="box"),
            ScratchOp(
                kind="set_parm",
                node_name="box1",
                parm="this_parm_does_not_exist",
                value=1.0,
            ),
        )
        try:
            executor.scratch_exec(scratch_request(invalid_id, invalid_ops))
        except Exception as exc:
            _expect("parm" in str(exc).lower(), f"invalid parm error is not diagnostic: {exc}")
        else:
            _die("invalid parm unexpectedly returned success")
        _expect(
            hou.node(f"/obj/eee_scratch_{invalid_id}") is not None,
            "invalid build did not preserve sandbox",
        )

        # 3. Health gate: an Add SOP with points and no primitives makes every
        # point orphaned, a deterministic hard failure.  This negative fixture
        # uses a native SOP outside the minimal catalog solely to exercise the
        # real gate; the valid path above is catalog-only.
        orphan_ops = (
            ScratchOp(kind="create_node", node_name="add1", node_type="add"),
            ScratchOp(
                kind="set_parm",
                node_name="add1",
                parm="points",
                value=3,
            ),
        )
        executor.scratch_exec(scratch_request(refused_id, orphan_ops))
        refused = executor.scratch_commit(
            commit_request(refused_id, name=f"{target_name}_refused")
        )
        _expect(refused.refused is True, "orphan points were not refused by health gate")
        _expect(refused.committed is False, "refused commit marked committed")
        _expect(
            hou.node(f"/obj/eee_scratch_{refused_id}") is not None,
            "refused sandbox was not preserved",
        )
        _expect(
            any(g.get("gate") == "health" and not g.get("passed") for g in refused.gates),
            "health gate failure is missing from refusal",
        )

        # 4. Orientation request without attributes must fail closed.
        executor.scratch_exec(
            scratch_request(
                orientation_id,
                (ScratchOp(kind="create_node", node_name="box1", node_type="box"),),
            )
        )
        orientation = executor.scratch_commit(
            commit_request(
                orientation_id,
                name=f"{target_name}_orientation",
                orientation_checks=(
                    {"component_id": "box", "expected_axis": "Y"},
                ),
            )
        )
        _expect(
            orientation.refused is True and orientation.committed is False,
            "orientation check without attributes did not fail closed",
        )
        _expect(
            any(g.get("gate") == "bake" and not g.get("passed") for g in orientation.gates),
            "bake gate did not report missing component attributes",
        )

        print("SCRATCH SMOKE OK")
        return 0
    finally:
        # Only paths using this process's unique prefixes are eligible for cleanup.
        for prefix in created_prefixes:
            for path in _created_node_paths(hou, prefix):
                node = hou.node(path)
                if node is not None:
                    with hou.undos.disabler():
                        node.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
