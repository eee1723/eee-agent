"""Disposable Houdini 21 Golden Case replay; run with hython, never pytest."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
for path in (ROOT, SITE_PACKAGES):
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


def main() -> None:
    import hou

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
    from eee_agent.modeling.golden_cases import houdini_21_minimal_golden_cases
    from eee_agent.modeling.validation import (
        ValidationStatus,
        validate_applied_scene,
    )
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    adapter = create_houdini_scene_adapter()
    executor = ChangeSetExecutor(adapter)
    roots: list[str] = []
    try:
        for index, case in enumerate(houdini_21_minimal_golden_cases(), start=1):
            root_name = f"eee_golden_{index}"
            root_path = f"/obj/{root_name}"
            roots.append(root_path)
            existing = hou.node(root_path)
            if existing is not None:
                existing.destroy()
            binding = adapter.binding()
            compiled = compile_bootstrap_procedural_spec(
                brief=case.brief,
                spec=case.spec,
                quality_profile=houdini_21_minimal_quality_profile(),
                catalog=houdini_21_minimal_catalog(),
                bootstrap=WorkspaceBootstrapContext(
                    workspace_id=f"ws_{index:032x}",
                    root_name=root_name,
                ),
                scene_binding=binding,
                session_id=f"ses_{'a' * 32}",
                run_id=f"run_{'b' * 32}",
                change_id=f"chg_{index:032x}",
                created_at=datetime.now(timezone.utc),
            )
            receipt = executor.apply(
                ApplyRequest.build(
                    request_id=f"golden_{index}",
                    deadline_ms=30_000,
                    scene_epoch=binding.scene_epoch,
                    changeset=compiled.changeset,
                    workspace=None,
                )
            )
            if receipt.status.value != "Applied":
                raise RuntimeError(
                    f"GOLDEN FAIL {case.case_id}: {receipt.to_dict()}"
                )
            query = adapter.scene_query(
                include_selection=False,
                node_paths=[node.path for node in compiled.changeset.affected_nodes],
                include_geometry_stats=True,
                expected_scene_epoch=binding.scene_epoch,
            )
            results = validate_applied_scene(
                changeset=compiled.changeset,
                query=query,
            )
            if any(item.status is not ValidationStatus.PASSED for item in results):
                raise RuntimeError(
                    f"GOLDEN FAIL {case.case_id}: "
                    f"{[item.to_dict() for item in results]}"
                )
            terminal = hou.node(f"{root_path}/{case.expected_terminal_node_name}")
            if terminal is None or terminal.geometry().primCount() <= 0:
                raise RuntimeError(
                    f"GOLDEN FAIL {case.case_id}: terminal output is empty"
                )
            manifest = derive_bootstrap_manifest(compiled.changeset, receipt)
            if len(manifest.nodes) != len(case.expected_node_types) + 1:
                raise RuntimeError(
                    f"GOLDEN FAIL {case.case_id}: manifest node count mismatch"
                )
        print(
            "GOLDEN CASES PASS: box, grid, merge, subdivide, polyextrude2, "
            "fuse2, normal, line, resample, sweep2, copytopoints2, outputs, "
            "Cook/Geometry, manifests"
        )
    finally:
        for root_path in roots:
            node = hou.node(root_path)
            if node is not None:
                node.destroy()
        adapter.close()


if __name__ == "__main__":
    main()
