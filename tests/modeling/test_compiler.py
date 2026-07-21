from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone

import pytest

from eee_agent.changesets import (
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    ParmValueEquals,
    PermissionMode,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WorkspaceRevisionEquals,
)
from eee_agent.changesets.policy import evaluate_policy
from eee_agent.changesets.contracts import OwnedNodeRef, WorkspaceManifest
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.compiler import (
    ModelingCompileError,
    NodeCatalog,
    NodeTypeDefinition,
    ParmDefinition,
    WorkspaceBootstrapContext,
    compile_bootstrap_procedural_spec,
    compile_procedural_spec,
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
    QualityProfile,
    UnitSystem,
    ValidatorKind,
)

SES = f"ses_{'1' * 32}"
RUN = f"run_{'2' * 32}"
WS = f"ws_{'3' * 32}"
CHG = f"chg_{'4' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = {
        "instance_id": "houdini_1",
        "scene_epoch": 7,
        "hip_path": None,
        "observed_revision": "scene-rev-7",
    }
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _workspace(**overrides: object) -> WorkspaceManifest:
    root = OwnedNodeRef(
        node_id="n_workspace",
        path="/obj/EEE_WORK",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    values: dict[str, object] = {
        "workspace_id": WS,
        "session_id": SES,
        "instance_id": "houdini_1",
        "scene_epoch": 7,
        "roots": (root,),
        "nodes": (root,),
        "created_by_run": RUN,
        "updated_at": NOW,
    }
    values.update(overrides)
    return WorkspaceManifest.build(**values)  # type: ignore[arg-type]


def _brief() -> ModelingBrief:
    return ModelingBrief(
        brief_key="simple_model",
        title="Simple model",
        asset_family="test_asset",
        goal="Build a deterministic two-node SOP graph.",
        units=UnitSystem.CENTIMETERS,
        up_axis=Axis.Y,
        front_axis=FrontAxis.NEGATIVE_Z,
        constraints=(),
    )


def _profile(**overrides: object) -> QualityProfile:
    values: dict[str, object] = {
        "profile_id": "strict_sop_v1",
        "validators": tuple(ValidatorKind),
        "max_compiled_nodes": 16,
        "max_parameter_samples": 16,
        "max_repairs_per_stage": 2,
        "allow_vex_source": False,
    }
    values.update(overrides)
    return QualityProfile(**values)  # type: ignore[arg-type]


def _catalog() -> NodeCatalog:
    return NodeCatalog(
        entries=(
            NodeTypeDefinition(
                node_type="geo",
                parameters=(),
                max_inputs=1,
                max_output_index=0,
                can_parent_nodes=True,
            ),
            NodeTypeDefinition(
                node_type="box",
                parameters=(
                    ParmDefinition("size", (1.0, 1.0, 1.0)),
                    ParmDefinition("center", (0.0, 0.0, 0.0)),
                ),
                max_inputs=0,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="xform",
                parameters=(
                    ParmDefinition("t", (0.0, 0.0, 0.0)),
                    ParmDefinition("scale", (1.0, 1.0, 1.0)),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="merge",
                parameters=(),
                max_inputs=8,
                max_output_index=0,
            ),
        )
    )


def _spec(brief: ModelingBrief | None = None, **overrides: object) -> ProceduralSpec:
    brief = brief or _brief()
    source = ComponentSpec(
        component_id="source",
        role="generator",
        depends_on=(),
        nodes=(
            NodeSpec(
                node_key="box",
                node_type="box",
                node_name="box1",
                parent_node=None,
                parameters=(ParmAssignment("size", (2.0, 3.0, 4.0)),),
                inputs=(),
            ),
        ),
    )
    finish = ComponentSpec(
        component_id="finish",
        role="transform",
        depends_on=("source",),
        nodes=(
            NodeSpec(
                node_key="xform",
                node_type="xform",
                node_name="xform1",
                parent_node=None,
                parameters=(ParmAssignment("t", (0.0, 1.0, 0.0)),),
                inputs=(InputBinding(0, "source.box", 0),),
            ),
        ),
    )
    values: dict[str, object] = {
        "spec_key": "simple_model_v1",
        "brief_digest": brief.digest,
        "quality_profile_id": "strict_sop_v1",
        "workspace_root_node_id": "n_workspace",
        "components": (source, finish),
    }
    values.update(overrides)
    return ProceduralSpec(**values)  # type: ignore[arg-type]


def _compile(**overrides: object):
    brief = overrides.pop("brief", _brief())
    spec = overrides.pop("spec", _spec(brief))
    values: dict[str, object] = {
        "brief": brief,
        "spec": spec,
        "quality_profile": _profile(),
        "catalog": _catalog(),
        "workspace": _workspace(),
        "scene_binding": _binding(),
        "session_id": SES,
        "run_id": RUN,
        "change_id": CHG,
        "created_at": NOW,
    }
    values.update(overrides)
    return compile_procedural_spec(**values)


def _compile_bootstrap(**overrides: object):
    brief = overrides.pop("brief", _brief())
    spec = overrides.pop(
        "spec", _spec(brief, workspace_root_node_id="bootstrap_root")
    )
    values: dict[str, object] = {
        "brief": brief,
        "spec": spec,
        "quality_profile": _profile(),
        "catalog": _catalog(),
        "bootstrap": WorkspaceBootstrapContext(
            workspace_id=WS,
            root_name="eee_model",
        ),
        "scene_binding": _binding(observed_revision="a" * 64),
        "session_id": SES,
        "run_id": RUN,
        "change_id": CHG,
        "created_at": NOW,
    }
    values.update(overrides)
    return compile_bootstrap_procedural_spec(**values)


def test_compile_is_deterministic_and_policy_allowed() -> None:
    first = _compile()
    second = _compile()
    assert first.to_dict() == second.to_dict()
    assert first.spec_digest == _spec().digest
    assert first.catalog_digest == _catalog().digest
    changeset = first.changeset
    assert changeset.required_permission is PermissionMode.OWNED_WORKSPACE
    assert changeset.workspace_id == WS
    assert evaluate_policy(changeset, workspace=_workspace()).allowed is True


def test_bootstrap_compile_is_deterministic_project_change() -> None:
    first = _compile_bootstrap()
    second = _compile_bootstrap()
    assert first.to_dict() == second.to_dict()
    changeset = first.changeset
    assert changeset.workspace_id is None
    assert changeset.required_permission is PermissionMode.PROJECT_CHANGE
    assert len(changeset.base_revision) == 64
    assert changeset.base_revision != changeset.scene_binding.observed_revision
    assert evaluate_policy(changeset, workspace=None).allowed is True
    assert [type(op) for op in changeset.operations] == [
        CreateNode,
        CreateNode,
        CreateNode,
        SetParm,
        SetParm,
        ConnectInput,
    ]
    root = changeset.operations[0]
    assert isinstance(root, CreateNode)
    assert root.parent.path == "/obj"
    assert root.node_type == "geo"
    assert root.node_name == "eee_model"
    assert root.workspace_id == WS
    assert changeset.affected_nodes[0].path == "/obj/eee_model"
    assert changeset.risk_summary.touches_external_nodes is True
    assert not any(
        isinstance(item, WorkspaceRevisionEquals)
        for item in changeset.preconditions
    )


def test_bootstrap_compile_binds_sentinel_catalog_and_workspace_identity() -> None:
    with pytest.raises(ModelingCompileError) as exc:
        _compile_bootstrap(spec=_spec(workspace_root_node_id="n_workspace"))
    assert exc.value.code == "modeling.bootstrap_root_invalid"

    catalog = NodeCatalog(
        entries=tuple(
            item for item in _catalog().entries if item.node_type != "geo"
        )
    )
    with pytest.raises(ModelingCompileError) as exc:
        _compile_bootstrap(catalog=catalog)
    assert exc.value.code == "modeling.bootstrap_catalog_invalid"

    other = WorkspaceBootstrapContext(
        workspace_id=f"ws_{'9' * 32}",
        root_name="eee_model",
    )
    assert (
        _compile_bootstrap().changeset.affected_nodes
        != _compile_bootstrap(bootstrap=other).changeset.affected_nodes
    )


def test_compile_derives_operations_defaults_conditions_and_risk() -> None:
    changeset = _compile().changeset
    assert [type(op) for op in changeset.operations] == [
        CreateNode,
        CreateNode,
        SetParm,
        SetParm,
        ConnectInput,
    ]
    parm_ops = [op for op in changeset.operations if isinstance(op, SetParm)]
    assert parm_ops[0].expected_old_value == (1.0, 1.0, 1.0)
    assert parm_ops[1].expected_old_value == (0.0, 0.0, 0.0)
    assert any(isinstance(item, SceneBindingEquals) for item in changeset.preconditions)
    assert any(isinstance(item, WorkspaceRevisionEquals) for item in changeset.preconditions)
    assert any(isinstance(item, NodeIdentityEquals) for item in changeset.preconditions)
    assert sum(isinstance(item, NodeAbsent) for item in changeset.preconditions) == 2
    assert sum(isinstance(item, NodeIdentityEquals) for item in changeset.expected_postconditions) == 2
    assert sum(isinstance(item, ParmValueEquals) for item in changeset.expected_postconditions) == 2
    assert sum(isinstance(item, WireInputEquals) for item in changeset.expected_postconditions) == 1
    assert changeset.checkpoint_plan.to_dict() == {
        "nodes": [
            {
                "node_id": "n_workspace",
                "path": "/obj/EEE_WORK",
                "expected_type": "geo",
                "expected_workspace_id": WS,
            }
        ],
        "parameters": [],
        "wires": [],
    }
    assert changeset.risk_summary.operation_count == 5
    assert changeset.risk_summary.touches_external_nodes is False
    assert changeset.risk_summary.requires_backup is False
    assert changeset.risk_summary.effect_names == (
        "node.create",
        "parm.set",
        "wire.connect",
    )


def test_compile_generates_stable_internal_ids_not_model_supplied_ids() -> None:
    changeset = _compile().changeset
    creates = [op for op in changeset.operations if isinstance(op, CreateNode)]
    assert all(op.node_id.startswith("n_") for op in creates)
    assert len({op.node_id for op in creates}) == 2
    assert all(op.op_id.startswith("op_create_") for op in creates)
    assert all(op.parent.path == "/obj/EEE_WORK" for op in creates)


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        (
            _spec(
                _brief(),
                brief_digest="f" * 64,
            ),
            "modeling.brief_mismatch",
        ),
        (
            _spec(
                _brief(),
                quality_profile_id="different",
            ),
            "modeling.profile_mismatch",
        ),
        (
            _spec(
                _brief(),
                workspace_root_node_id="missing_root",
            ),
            "modeling.workspace_mismatch",
        ),
    ],
)
def test_compile_rejects_binding_mismatches(
    spec: ProceduralSpec, code: str
) -> None:
    with pytest.raises(ModelingCompileError) as caught:
        _compile(spec=spec)
    assert caught.value.code == code


def test_compile_rejects_catalog_node_parm_and_shape_injection() -> None:
    brief = _brief()
    base = _spec(brief)
    component = base.components[0]
    bad_node = NodeSpec(
        node_key="bad",
        node_type="python",
        node_name="bad",
        parent_node=None,
        parameters=(),
        inputs=(),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(
            brief=brief,
            spec=_spec(
                brief,
                components=(
                    ComponentSpec("source", "generator", (), (bad_node,)),
                ),
            ),
        )
    assert caught.value.code == "modeling.catalog_node_denied"

    bad_parm = NodeSpec(
        node_key="box",
        node_type="box",
        node_name="box1",
        parent_node=None,
        parameters=(ParmAssignment("python", "hou.node('/').destroy()"),),
        inputs=(),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(
            brief=brief,
            spec=_spec(
                brief,
                components=(ComponentSpec("source", "generator", (), (bad_parm,)),),
            ),
        )
    assert caught.value.code == "modeling.catalog_parm_denied"

    wrong_shape = NodeSpec(
        node_key="box",
        node_type="box",
        node_name="box1",
        parent_node=None,
        parameters=(ParmAssignment("size", (1, 2, 3)),),
        inputs=(),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(
            brief=brief,
            spec=_spec(
                brief,
                components=(ComponentSpec("source", "generator", (), (wrong_shape,)),),
            ),
        )
    assert caught.value.code == "modeling.catalog_value_mismatch"


def test_compile_rejects_node_cycle_and_input_ranges() -> None:
    brief = _brief()
    cyclic = ComponentSpec(
        component_id="cycle",
        role="bad",
        depends_on=(),
        nodes=(
            NodeSpec(
                "a", "xform", "a", None, (), (InputBinding(0, "cycle.b", 0),)
            ),
            NodeSpec(
                "b", "xform", "b", None, (), (InputBinding(0, "cycle.a", 0),)
            ),
        ),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(brief=brief, spec=_spec(brief, components=(cyclic,)))
    assert caught.value.code == "modeling.dependency_cycle"

    source = ComponentSpec(
        "source",
        "generator",
        (),
        (
            NodeSpec("box", "box", "box1", None, (), ()),
        ),
    )
    target = ComponentSpec(
        "target",
        "consumer",
        ("source",),
        (
            NodeSpec(
                "xform",
                "xform",
                "xform1",
                None,
                (),
                (InputBinding(1, "source.box", 0),),
            ),
        ),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(brief=brief, spec=_spec(brief, components=(source, target)))
    assert caught.value.code == "modeling.input_out_of_range"


def test_compile_rejects_profile_node_budget_and_scene_mismatch() -> None:
    with pytest.raises(ModelingCompileError) as caught:
        _compile(quality_profile=_profile(max_compiled_nodes=1))
    assert caught.value.code == "modeling.node_budget_exceeded"
    with pytest.raises(ModelingCompileError) as caught:
        _compile(scene_binding=_binding(scene_epoch=8))
    assert caught.value.code == "modeling.workspace_mismatch"


def test_compile_rejects_typed_changeset_operation_budget_overflow() -> None:
    brief = _brief()
    parameters = tuple(ParmDefinition(f"p{index}", 0) for index in range(4))
    catalog = NodeCatalog(
        entries=(
            NodeTypeDefinition(
                node_type="box",
                parameters=parameters,
                max_inputs=0,
                max_output_index=0,
            ),
        )
    )
    nodes = tuple(
        NodeSpec(
            node_key=f"n{index}",
            node_type="box",
            node_name=f"n{index}",
            parent_node=None,
            parameters=tuple(
                ParmAssignment(f"p{parm_index}", 1) for parm_index in range(4)
            ),
            inputs=(),
        )
        for index in range(64)
    )
    spec = ProceduralSpec(
        spec_key="too_many_operations",
        brief_digest=brief.digest,
        quality_profile_id="strict_sop_v1",
        workspace_root_node_id="n_workspace",
        components=(ComponentSpec("bulk", "primary", (), nodes),),
    )
    with pytest.raises(ModelingCompileError) as caught:
        _compile(
            brief=brief,
            spec=spec,
            catalog=catalog,
            quality_profile=_profile(max_compiled_nodes=64),
        )
    assert caught.value.code == "modeling.operation_budget_exceeded"


def test_catalog_alias_resolves_only_trusted_versioned_create_type() -> None:
    definition = NodeTypeDefinition(
        node_type="polyextrude2",
        create_type="polyextrude::2.0",
        parameters=(),
        max_inputs=2,
        max_output_index=0,
    )
    assert definition.node_type == "polyextrude2"
    assert definition.create_type == "polyextrude::2.0"
    with pytest.raises(ValueError):
        NodeTypeDefinition(
            node_type="bad_alias",
            create_type="../../python",
            parameters=(),
            max_inputs=1,
            max_output_index=0,
        )


def test_compiler_module_has_no_houdini_runtime_or_dynamic_execution_imports() -> None:
    import eee_agent.modeling.compiler as compiler

    tree = ast.parse(inspect.getsource(compiler))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name == "hou"
        or name == "rpyc"
        or name.startswith("eee_agent.runtime.service")
        or name.startswith("eee_agent.bridge")
        or name.startswith("langchain")
        or name.startswith("langgraph")
        for name in imported
    )
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls.isdisjoint({"eval", "exec", "compile", "open", "__import__"})
