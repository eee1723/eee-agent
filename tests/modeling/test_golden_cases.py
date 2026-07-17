from __future__ import annotations

from datetime import datetime, timezone

from eee_agent.changesets.contracts import CreateNode
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.catalog import (
    houdini_21_minimal_catalog,
    houdini_21_minimal_quality_profile,
)
from eee_agent.modeling.compiler import (
    WorkspaceBootstrapContext,
    compile_bootstrap_procedural_spec,
)
from eee_agent.modeling.golden_cases import houdini_21_minimal_golden_cases
from eee_agent.modeling.validation import validate_compilation


def _compile(case, index: int):
    return compile_bootstrap_procedural_spec(
        brief=case.brief,
        spec=case.spec,
        quality_profile=houdini_21_minimal_quality_profile(),
        catalog=houdini_21_minimal_catalog(),
        bootstrap=WorkspaceBootstrapContext(
            workspace_id=f"ws_{index:032x}",
            root_name=f"eee_golden_{case.case_id}",
        ),
        scene_binding=SceneBinding(
            instance_id="golden_houdini",
            scene_epoch=1,
            hip_path=None,
            observed_revision="golden-scene",
        ),
        session_id=f"ses_{'a' * 32}",
        run_id=f"run_{'b' * 32}",
        change_id=f"chg_{index:032x}",
        created_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )


def test_minimal_golden_cases_compile_deterministically() -> None:
    cases = houdini_21_minimal_golden_cases()
    assert [case.case_id for case in cases] == [
        "box_transform_output",
        "grid_transform_output",
        "merged_sources_output",
        "subdivided_surface_output",
        "extruded_grid_output",
        "copied_box_output",
        "swept_lines_output",
    ]
    for index, case in enumerate(cases, start=1):
        first = _compile(case, index)
        second = _compile(case, index)
        assert first == second
        created_types = tuple(
            operation.node_type
            for operation in first.changeset.operations
            if isinstance(operation, CreateNode)
        )
        catalog = houdini_21_minimal_catalog()
        expected_create_types = tuple(
            catalog.by_type[node_type].create_type
            for node_type in case.expected_node_types
        )
        assert created_types == ("geo", *expected_create_types)
        assert any(
            node.path.endswith(f"/{case.expected_terminal_node_name}")
            for node in first.changeset.affected_nodes
        )
        report = validate_compilation(
            brief=case.brief,
            spec=case.spec,
            quality_profile=houdini_21_minimal_quality_profile(),
            catalog=houdini_21_minimal_catalog(),
            compilation=first,
        )
        assert report.hard_failures == ()
