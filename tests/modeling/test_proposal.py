from __future__ import annotations

import asyncio
import ast
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from eee_agent.changesets.contracts import OwnedNodeRef, WorkspaceManifest
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.compiler import (
    NodeCatalog,
    NodeTypeDefinition,
    ParmDefinition,
    WorkspaceBootstrapContext,
)
from eee_agent.modeling.contracts import (
    Axis,
    ComponentSpec,
    FrontAxis,
    ModelingBrief,
    NodeSpec,
    ParmAssignment,
    ProceduralSpec,
    QualityProfile,
    UnitSystem,
    ValidatorKind,
)
from eee_agent.modeling.proposal import (
    ModelingProposalContext,
    ModelingProposalCoordinator,
    ModelingProposalError,
    ModelingToolContext,
    propose_modeling,
)
from eee_agent.runtime.agent_context import RuntimeToolContext


class _ReadOnlyProvider:
    async def scene_status(self):
        return {}

    async def query_scene(self, node_paths):
        return {}

    async def inspect_workspace(self, workspace_id):
        return {}

    async def geometry_stats(self, node_path):
        return {}

    async def work_status(self, workspace_id):
        return {}


class _Knowledge:
    def search(self, query, *, limit=5):
        return {"ok": True, "results": []}

    def get(self, entity_id, *, max_body_bytes=8_000):
        return {"ok": True, "entity_id": entity_id}

SES = f"ses_{'1' * 32}"
RUN = f"run_{'2' * 32}"
WS = f"ws_{'3' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _brief() -> ModelingBrief:
    return ModelingBrief(
        brief_key="proposal",
        title="Proposal",
        asset_family="test_asset",
        goal="Build one bounded node.",
        units=UnitSystem.CENTIMETERS,
        up_axis=Axis.Y,
        front_axis=FrontAxis.NEGATIVE_Z,
        constraints=(),
    )


def _spec(brief: ModelingBrief) -> ProceduralSpec:
    return ProceduralSpec(
        spec_key="proposal_v1",
        brief_digest=brief.digest,
        quality_profile_id="strict_sop_v1",
        workspace_root_node_id="n_workspace",
        components=(
            ComponentSpec(
                component_id="body",
                role="primary",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="box",
                        node_type="box",
                        node_name="box1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("size", (2.0, 3.0, 4.0)),
                        ),
                        inputs=(),
                    ),
                ),
            ),
        ),
    )


def _profile() -> QualityProfile:
    return QualityProfile(
        profile_id="strict_sop_v1",
        validators=tuple(ValidatorKind),
        max_compiled_nodes=16,
        max_parameter_samples=8,
        max_repairs_per_stage=2,
        allow_vex_source=False,
    )


def _catalog() -> NodeCatalog:
    return NodeCatalog(
        entries=(
            NodeTypeDefinition(
                node_type="box",
                parameters=(ParmDefinition("size", (1.0, 1.0, 1.0)),),
                max_inputs=0,
                max_output_index=0,
            ),
        )
    )


def _bootstrap_catalog() -> NodeCatalog:
    return NodeCatalog(
        entries=(
            NodeTypeDefinition(
                node_type="geo",
                parameters=(),
                max_inputs=1,
                max_output_index=0,
                can_parent_nodes=True,
            ),
            *_catalog().entries,
        )
    )


def _workspace() -> WorkspaceManifest:
    root = OwnedNodeRef(
        node_id="n_workspace",
        path="/obj/EEE_WORK",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="houdini_1",
        scene_epoch=1,
        roots=(root,),
        nodes=(root,),
        created_by_run=RUN,
        updated_at=NOW,
    )


def _context(callback, *, change_id: str = f"chg_{'4' * 32}") -> ModelingProposalContext:
    return ModelingProposalContext(
        session_id=SES,
        run_id=RUN,
        workspace=_workspace(),
        scene_binding=SceneBinding(
            instance_id="houdini_1",
            scene_epoch=1,
            hip_path=None,
            observed_revision="scene-1",
        ),
        catalog=_catalog(),
        quality_profile=_profile(),
        propose_callback=callback,
        clock=lambda: NOW,
        change_id_factory=lambda: change_id,
    )


def _bootstrap_context(callback) -> ModelingProposalContext:
    return ModelingProposalContext(
        session_id=SES,
        run_id=RUN,
        workspace=None,
        bootstrap=WorkspaceBootstrapContext(
            workspace_id=WS,
            root_name="eee_model",
        ),
        scene_binding=SceneBinding(
            instance_id="houdini_1",
            scene_epoch=1,
            hip_path=None,
            observed_revision="a" * 64,
        ),
        catalog=_bootstrap_catalog(),
        quality_profile=_profile(),
        propose_callback=callback,
        clock=lambda: NOW,
        change_id_factory=lambda: f"chg_{'8' * 32}",
    )


def _run(coro):
    return asyncio.run(coro)


def test_coordinator_compiles_and_calls_trusted_callback_once() -> None:
    calls: list[tuple[object, object]] = []

    async def callback(changeset, decision):
        calls.append((changeset, decision))

    brief = _brief()
    summary = _run(
        ModelingProposalCoordinator(_context(callback)).propose(
            brief_data=brief.to_dict(),
            spec_data=_spec(brief).to_dict(),
        )
    )
    assert summary.state == "AwaitingApproval"
    assert summary.approval_required is True
    assert summary.operation_count == 2
    assert summary.workspace_id == WS
    assert len(calls) == 1
    assert calls[0][0].digest == summary.changeset_digest
    assert calls[0][1].allowed is True
    assert "operations" not in summary.to_dict()
    assert "parameter" not in str(summary.to_dict()).lower()


def test_coordinator_compiles_empty_scene_bootstrap_proposal() -> None:
    calls: list[tuple[object, object]] = []

    async def callback(changeset, decision):
        calls.append((changeset, decision))

    brief = _brief()
    spec = _spec(brief).to_dict()
    spec["workspace_root_node_id"] = "bootstrap_root"
    summary = _run(
        ModelingProposalCoordinator(_bootstrap_context(callback)).propose(
            brief_data=brief.to_dict(),
            spec_data=spec,
        )
    )
    assert summary.workspace_id == WS
    assert summary.state == "AwaitingApproval"
    assert summary.operation_count == 3
    changeset, decision = calls[0]
    assert changeset.workspace_id is None
    assert changeset.operations[0].node_type == "geo"
    assert decision.allowed is True


def test_coordinator_round_trips_model_json_without_returning_raw_changeset() -> None:
    calls: list[object] = []

    def callback(changeset, decision):
        calls.append(changeset)

    brief = _brief()
    result = _run(
        ModelingProposalCoordinator(_context(callback)).propose(
            brief_data=brief.to_dict(),
            spec_data=_spec(brief).to_dict(),
        )
    )
    assert len(calls) == 1
    assert set(result.to_dict()) == {
        "change_id",
        "changeset_digest",
        "workspace_id",
        "state",
        "approval_required",
        "operation_count",
        "effect_names",
        "affected_path_count",
    }


def test_coordinator_rejects_invalid_input_before_callback() -> None:
    calls: list[object] = []
    brief = _brief()

    def callback(changeset, decision):
        calls.append(changeset)

    with pytest.raises(ModelingProposalError) as caught:
        _run(
            ModelingProposalCoordinator(_context(callback)).propose(
                brief_data=brief.to_dict(),
                spec_data={"schema_version": 1, "operations": []},
            )
        )
    assert caught.value.code == "modeling.proposal_input_invalid"
    assert calls == []


def test_coordinator_ignores_model_supplied_trusted_binding_fields() -> None:
    calls: list[object] = []

    def callback(changeset, decision):
        calls.append(changeset)

    brief = _brief()
    bad = _spec(brief).to_dict()
    bad["brief_digest"] = "f" * 64
    bad["quality_profile_id"] = "invented_profile"
    bad["workspace_root_node_id"] = "invented_root"
    summary = _run(
        ModelingProposalCoordinator(_context(callback)).propose(
            brief_data=brief.to_dict(),
            spec_data=bad,
        )
    )
    assert summary.workspace_id == WS
    assert len(calls) == 1
    assert calls[0].workspace_id == WS


def test_coordinator_injects_omitted_trusted_binding_fields() -> None:
    calls: list[object] = []
    brief = _brief()
    model_spec = _spec(brief).to_dict()
    for field in (
        "brief_digest",
        "quality_profile_id",
        "workspace_root_node_id",
    ):
        model_spec.pop(field)
    summary = _run(
        ModelingProposalCoordinator(
            _context(lambda changeset, _decision: calls.append(changeset))
        ).propose(brief_data=brief.to_dict(), spec_data=model_spec)
    )
    assert summary.workspace_id == WS
    assert len(calls) == 1


def test_coordinator_never_retries_persist_callback() -> None:
    calls = 0

    async def callback(changeset, decision):
        nonlocal calls
        calls += 1
        raise RuntimeError("database detail must not escape")

    brief = _brief()
    with pytest.raises(ModelingProposalError) as caught:
        _run(
            ModelingProposalCoordinator(_context(callback)).propose(
                brief_data=brief.to_dict(),
                spec_data=_spec(brief).to_dict(),
            )
        )
    assert caught.value.code == "modeling.proposal_persist_failed"
    assert calls == 1
    assert "database detail" not in str(caught.value)


def test_tool_hides_runtime_context_and_returns_bounded_summary() -> None:
    calls: list[object] = []

    def callback(changeset, decision):
        calls.append(changeset)

    modeling = ModelingToolContext(ModelingProposalCoordinator(_context(callback)))
    context = RuntimeToolContext(read_only=_ReadOnlyProvider(), knowledge=_Knowledge(), modeling=modeling)
    brief = _brief()
    result = _run(
        propose_modeling.coroutine(  # type: ignore[union-attr]
            brief=brief.to_dict(),
            spec=_spec(brief).to_dict(),
            runtime=SimpleNamespace(context=context),
        )
    )
    assert result["ok"] is True
    assert "proposal" in result
    assert len(calls) == 1
    assert "runtime" not in propose_modeling.args
    assert "operations" not in str(result)
    assert "size" not in str(result)


def test_tool_fails_closed_without_trusted_context() -> None:
    brief = _brief()
    result = _run(
        propose_modeling.coroutine(  # type: ignore[union-attr]
            brief=brief.to_dict(),
            spec=_spec(brief).to_dict(),
            runtime=SimpleNamespace(context=None),
        )
    )
    assert result == {
        "ok": False,
        "code": "modeling.proposal_context_invalid",
        "message": "A trusted modeling context is unavailable.",
    }


def test_proposal_tool_reads_modeling_context_from_runtime_context() -> None:
    calls: list[object] = []

    def callback(changeset, decision):
        calls.append((changeset, decision))

    brief = _brief()
    context = RuntimeToolContext(
        read_only=_ReadOnlyProvider(),
        knowledge=_Knowledge(),
        modeling=ModelingToolContext(ModelingProposalCoordinator(_context(callback))),
    )
    result = _run(
        propose_modeling.coroutine(  # type: ignore[union-attr]
            brief=brief.to_dict(),
            spec=_spec(brief).to_dict(),
            runtime=SimpleNamespace(context=context),
        )
    )
    assert result["ok"] is True
    assert len(calls) == 1


def test_proposal_module_has_no_houdini_runtime_or_dynamic_execution_imports() -> None:
    import eee_agent.modeling.proposal as proposal

    tree = ast.parse(inspect.getsource(proposal))
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
        or name.startswith("eee_agent.bridge")
        or name.startswith("sqlite")
        or name.startswith("subprocess")
        for name in imported
    )
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls.isdisjoint({"eval", "exec", "open", "__import__"})


def test_tool_docstring_minimal_skeleton_parses() -> None:
    """The example embedded in the propose_modeling docstring stays valid.

    The docstring IS the tool description sent to the model; its minimal
    skeleton must parse through the same strict from_dict validators, with
    the Runtime-injected spec keys filled exactly as the coordinator does.
    """
    brief_data = {
        "schema_version": 1,
        "brief_key": "table",
        "title": "Parametric table",
        "asset_family": "furniture",
        "goal": "Simple table with a box top",
        "units": "Centimeters",
        "up_axis": "Y",
        "front_axis": "NegativeZ",
        "constraints": [],
    }
    spec_data = {
        "schema_version": 1,
        "spec_key": "table_v1",
        "components": [
            {
                "component_id": "top",
                "role": "primary",
                "depends_on": [],
                "nodes": [
                    {
                        "node_key": "top_box",
                        "node_type": "box",
                        "node_name": "tabletop",
                        "parent_node": None,
                        "parameters": [
                            {"parm_name": "sizex", "value": 120.0},
                            {"parm_name": "sizey", "value": 4.0},
                            {"parm_name": "sizez", "value": 70.0},
                        ],
                        "inputs": [],
                    }
                ],
            }
        ],
    }
    brief = ModelingBrief.from_dict(brief_data)
    trusted_spec = dict(spec_data)
    trusted_spec["brief_digest"] = brief.digest
    trusted_spec["quality_profile_id"] = "strict_sop_v1"
    trusted_spec["workspace_root_node_id"] = "n_workspace"
    spec = ProceduralSpec.from_dict(trusted_spec)
    assert spec.components[0].nodes[0].node_type == "box"
    # The catalog node types named in the docstring exist in the production
    # minimal catalog.
    from eee_agent.modeling.catalog import houdini_21_minimal_catalog

    production_types = {
        entry.node_type for entry in houdini_21_minimal_catalog().entries
    }
    assert {
        "box", "grid", "line", "normal", "fuse2", "polyextrude2", "subdivide",
        "resample", "sweep2", "xform", "null", "merge", "copytopoints2",
        "boolean2", "geo",
    } <= production_types
