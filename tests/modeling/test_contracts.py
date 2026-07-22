from __future__ import annotations

import json

import pytest

from eee_agent.modeling.contracts import (
    Axis,
    BriefConstraint,
    ComponentSpec,
    FrontAxis,
    InputBinding,
    ModelingBrief,
    NodeSpec,
    ParmAssignment,
    ProceduralSpec,
    QualityProfile,
    RepairAttempt,
    RepairBudget,
    RepairStatus,
    RepairTicket,
    UnitSystem,
    ValidatorKind,
    parse_modeling_brief,
    parse_procedural_spec,
    parse_quality_profile,
    parse_repair_budget,
    parse_repair_ticket,
)


def _brief(**overrides: object) -> ModelingBrief:
    values: dict[str, object] = {
        "brief_key": "table",
        "title": "Parametric table",
        "asset_family": "furniture",
        "goal": "Build a stable component-based table.",
        "units": UnitSystem.CENTIMETERS,
        "up_axis": Axis.Y,
        "front_axis": FrontAxis.NEGATIVE_Z,
        "constraints": (
            BriefConstraint(
                code="semantic.four_legs",
                statement="The table must have four supporting legs.",
            ),
        ),
    }
    values.update(overrides)
    return ModelingBrief(**values)  # type: ignore[arg-type]


def _node(**overrides: object) -> NodeSpec:
    values: dict[str, object] = {
        "node_key": "box",
        "node_type": "box",
        "node_name": "tabletop",
        "parent_node": None,
        "parameters": (
            ParmAssignment(parm_name="size", value=(100.0, 5.0, 60.0)),
        ),
        "inputs": (),
    }
    values.update(overrides)
    return NodeSpec(**values)  # type: ignore[arg-type]


def _spec(brief: ModelingBrief | None = None, **overrides: object) -> ProceduralSpec:
    brief = brief or _brief()
    values: dict[str, object] = {
        "spec_key": "table_v1",
        "brief_digest": brief.digest,
        "quality_profile_id": "strict_sop_v1",
        "workspace_root_node_id": "n_workspace",
        "components": (
            ComponentSpec(
                component_id="body",
                role="primary",
                depends_on=(),
                nodes=(_node(),),
            ),
        ),
    }
    values.update(overrides)
    return ProceduralSpec(**values)  # type: ignore[arg-type]


def _profile(**overrides: object) -> QualityProfile:
    values: dict[str, object] = {
        "profile_id": "strict_sop_v1",
        "validators": tuple(ValidatorKind),
        "max_compiled_nodes": 64,
        "max_parameter_samples": 32,
        "max_repairs_per_stage": 2,
        "allow_vex_source": False,
    }
    values.update(overrides)
    return QualityProfile(**values)  # type: ignore[arg-type]


def test_brief_round_trip_digest_and_fresh_tree() -> None:
    brief = _brief()
    parsed = parse_modeling_brief(
        json.dumps(brief.to_dict(), ensure_ascii=False)
    )
    assert parsed == brief
    assert parsed.digest == brief.digest
    first = parsed.to_dict()
    first["constraints"][0]["statement"] = "mutated"  # type: ignore[index]
    assert parsed.to_dict()["constraints"][0]["statement"].startswith("The table")  # type: ignore[index]


def test_brief_strict_json_preserves_unicode() -> None:
    brief = _brief(title="参数化桌子", goal="创建稳定、可验证的程序化桌子。")
    parsed = parse_modeling_brief(
        json.dumps(brief.to_dict(), ensure_ascii=False)
    )
    assert parsed.title == "参数化桌子"
    assert parsed.goal.endswith("桌子。")


def test_brief_rejects_duplicate_json_key() -> None:
    raw = json.dumps(
        _brief().to_dict(), ensure_ascii=False, separators=(",", ":")
    )
    raw = raw.replace('"brief_key":"table"', '"brief_key":"x","brief_key":"table"')
    with pytest.raises(ValueError, match="strict JSON"):
        parse_modeling_brief(raw)


def test_brief_rejects_parallel_up_and_front_axes() -> None:
    with pytest.raises(ValueError, match="front_axis"):
        _brief(front_axis=FrontAxis.POSITIVE_Y)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"x": 1}])
def test_parm_assignment_rejects_unsafe_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ParmAssignment(parm_name="size", value=value)


def test_node_rejects_duplicate_parm_and_input_slots() -> None:
    with pytest.raises(ValueError, match="duplicate parameter"):
        _node(
            parameters=(
                ParmAssignment("size", (1.0, 1.0, 1.0)),
                ParmAssignment("size", (2.0, 2.0, 2.0)),
            )
        )
    with pytest.raises(ValueError, match="duplicate input"):
        _node(
            inputs=(
                InputBinding(0, "body.source", 0),
                InputBinding(0, "body.other", 0),
            )
        )


def test_spec_rejects_unknown_and_hidden_component_dependencies() -> None:
    brief = _brief()
    with pytest.raises(ValueError, match="unknown component"):
        _spec(
            brief,
            components=(
                ComponentSpec(
                    component_id="body",
                    role="primary",
                    depends_on=("missing",),
                    nodes=(_node(),),
                ),
            ),
        )

    source = ComponentSpec(
        component_id="source",
        role="source",
        depends_on=(),
        nodes=(_node(node_key="src", node_name="src"),),
    )
    consumer = ComponentSpec(
        component_id="consumer",
        role="consumer",
        depends_on=(),
        nodes=(
            _node(
                node_key="dst",
                node_name="dst",
                inputs=(InputBinding(0, "source.src", 0),),
            ),
        ),
    )
    with pytest.raises(ValueError, match="hidden dependency"):
        _spec(brief, components=(source, consumer))


def test_spec_rejects_component_cycle_and_bad_qualified_reference() -> None:
    brief = _brief()
    a = ComponentSpec(
        component_id="a",
        role="part",
        depends_on=("b",),
        nodes=(_node(node_key="a", node_name="a"),),
    )
    b = ComponentSpec(
        component_id="b",
        role="part",
        depends_on=("a",),
        nodes=(_node(node_key="b", node_name="b"),),
    )
    with pytest.raises(ValueError, match="cycle"):
        _spec(brief, components=(a, b))
    with pytest.raises(ValueError, match="qualified"):
        InputBinding(0, "/obj/injected", 0)


def test_spec_strict_json_rejects_extra_fields() -> None:
    payload = _spec().to_dict()
    payload["operations"] = [{"kind": "node.delete"}]
    with pytest.raises(ValueError, match="exactly"):
        parse_procedural_spec(json.dumps(payload))


def test_profile_requires_all_deterministic_validators_and_max_two_repairs() -> None:
    profile = _profile()
    assert parse_quality_profile(json.dumps(profile.to_dict())) == profile
    assert profile.validators == tuple(ValidatorKind)
    with pytest.raises(ValueError, match="validator order"):
        _profile(validators=(ValidatorKind.SPEC_CONTRACT,))
    with pytest.raises(ValueError, match="at most 2"):
        _profile(max_repairs_per_stage=3)
    with pytest.raises(ValueError, match="VEX"):
        _profile(allow_vex_source=True)


def test_repair_budget_is_immutable_monotonic_and_bounded() -> None:
    budget = RepairBudget(max_attempts_per_stage=2, attempts=())
    once = budget.record(ValidatorKind.GRAPH)
    twice = once.record(ValidatorKind.GRAPH)
    assert budget.used(ValidatorKind.GRAPH) == 0
    assert once.remaining(ValidatorKind.GRAPH) == 1
    assert twice.remaining(ValidatorKind.GRAPH) == 0
    assert parse_repair_budget(json.dumps(twice.to_dict())) == twice
    with pytest.raises(ValueError, match="exhausted"):
        twice.record(ValidatorKind.GRAPH)
    with pytest.raises(ValueError):
        RepairBudget(
            max_attempts_per_stage=2,
            attempts=(RepairAttempt(ValidatorKind.GRAPH, 3),),
        )


def test_repair_ticket_is_bounded_and_versioned() -> None:
    ticket = RepairTicket(
        ticket_id="repair_graph_1",
        validator=ValidatorKind.GRAPH,
        failure_code="validator.graph_mismatch",
        message="The reconciled graph differs from the spec.",
        evidence_digests=("a" * 64,),
        failed_parameter_samples=("width=max",),
        replay_boundary_digest="b" * 64,
        attempt=2,
        status=RepairStatus.OPEN,
    )
    assert ticket.to_dict()["attempt"] == 2
    assert parse_repair_ticket(json.dumps(ticket.to_dict())) == ticket
    with pytest.raises(ValueError, match="attempt"):
        RepairTicket(
            ticket_id="repair_graph_3",
            validator=ValidatorKind.GRAPH,
            failure_code="validator.graph_mismatch",
            message="bad",
            evidence_digests=(),
            failed_parameter_samples=(),
            replay_boundary_digest="b" * 64,
            attempt=3,
            status=RepairStatus.OPEN,
        )


def test_contract_module_has_no_write_or_framework_imports() -> None:
    import ast
    import inspect

    import eee_agent.modeling.contracts as contracts

    tree = ast.parse(inspect.getsource(contracts))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported |= {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported.isdisjoint(
        {"hou", "rpyc", "langchain", "langgraph", "subprocess", "sqlite3"}
    )


# ---------------------------------------------------------------------------
# Exception-direction regression: from_dict must preserve the original
# ValueError vs TypeError direction that each field's __post_init__ defines.
# These exercises the parse_*/from_dict boundary (the real data entry point).
# ---------------------------------------------------------------------------


def _brief_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "brief_key": "table",
        "title": "Parametric table",
        "asset_family": "furniture",
        "goal": "Build a stable component-based table.",
        "units": UnitSystem.CENTIMETERS.value,
        "up_axis": Axis.Y.value,
        "front_axis": FrontAxis.NEGATIVE_Z.value,
        "constraints": (
            BriefConstraint(
                code="semantic.four_legs",
                statement="The table must have four supporting legs.",
            ).to_dict(),
        ),
    }
    values.update(overrides)
    return values


def _profile_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": 1,
        "profile_id": "strict_sop_v1",
        "validators": [v.value for v in ValidatorKind],
        "max_compiled_nodes": 64,
        "max_parameter_samples": 32,
        "max_repairs_per_stage": 2,
        "allow_vex_source": False,
    }
    values.update(overrides)
    return values


def _attempt_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "validator": ValidatorKind.GRAPH.value,
        "count": 1,
    }
    values.update(overrides)
    return values


def _node_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "node_key": "box",
        "node_type": "box",
        "node_name": "tabletop",
        "parent_node": None,
        "parameters": (
            ParmAssignment(parm_name="size", value=(100.0, 5.0, 60.0)).to_dict(),
        ),
        "inputs": [],
    }
    values.update(overrides)
    return values


def _binding_dict(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "input_index": 0,
        "source_node": "body.source",
        "source_output_index": 0,
    }
    values.update(overrides)
    return values


def test_brief_units_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="units"):
        parse_modeling_brief(json.dumps(_brief_dict(units=123)))


def test_brief_up_axis_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="up_axis"):
        parse_modeling_brief(json.dumps(_brief_dict(up_axis=123)))


def test_brief_front_axis_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="front_axis"):
        parse_modeling_brief(json.dumps(_brief_dict(front_axis=123)))


def test_brief_schema_version_bool_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        parse_modeling_brief(json.dumps(_brief_dict(schema_version=True)))


def test_brief_brief_key_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="brief_key"):
        parse_modeling_brief(json.dumps(_brief_dict(brief_key=123)))


def test_profile_validators_non_str_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="validators"):
        parse_quality_profile(json.dumps(_profile_dict(validators=[123])))


def test_profile_profile_id_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="profile_id"):
        parse_quality_profile(json.dumps(_profile_dict(profile_id=123)))


def test_profile_max_compiled_nodes_bool_rejected_as_type_error() -> None:
    with pytest.raises(TypeError, match="max_compiled_nodes"):
        parse_quality_profile(
            json.dumps(_profile_dict(max_compiled_nodes=True))
        )


def test_attempt_validator_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="validator"):
        RepairAttempt.from_dict(_attempt_dict(validator=123))


def test_attempt_count_bool_rejected_as_type_error() -> None:
    with pytest.raises(TypeError, match="count"):
        RepairAttempt.from_dict(_attempt_dict(count=True))


def test_node_parent_node_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="parent_node"):
        NodeSpec.from_dict(_node_dict(parent_node=123))


def test_node_node_key_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="node_key"):
        NodeSpec.from_dict(_node_dict(node_key=123))


def test_binding_source_node_int_rejected_as_value_error() -> None:
    with pytest.raises(ValueError, match="source_node"):
        InputBinding.from_dict(_binding_dict(source_node=123))


def test_binding_input_index_bool_rejected_as_type_error() -> None:
    with pytest.raises(TypeError, match="input_index"):
        InputBinding.from_dict(_binding_dict(input_index=True))
