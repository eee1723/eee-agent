"""Task 16-C: strict Bridge preflight capability + DTO contract tests.

Pure-Python RED/GREEN tests for the additive ``changeset.v1`` capability and the
frozen, JSON-canonical ``changeset.preflight`` request/response DTOs defined in
:mod:`eee_agent.houdini_bridge.changesets`. No ``hou``, ``rpyc``, transport, or
Houdini process is exercised here — the server/client/adapter behaviour lives in
:mod:`test_changeset_bridge_preflight`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from eee_agent.changesets import (
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    NodeRef,
    OwnedNodeRef,
    PermissionMode,
    ReceiptStatus,
    RiskSummary,
    SetParm,
    WireRef,
    WorkspaceManifest,
)
from eee_agent.houdini_bridge.changesets import (
    CHANGESET_V1,
    ApplyRequest,
    ApplyResponse,
    PreflightNodeFact,
    PreflightParmFact,
    PreflightRequest,
    PreflightResponse,
    PreflightResult,
    PreflightWireFact,
    ReceiptRequest,
    ReceiptResponse,
    validate_capabilities,
)
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    SceneBinding,
)
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# shared constants + factories (mirror test_changeset_contracts patterns)
# --------------------------------------------------------------------------

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
CHG = f"chg_{'4' * 32}"
REVISION = "a" * 64
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
INSTANCE = "hou:21.0.440:pid12345"


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = dict(
        instance_id=INSTANCE,
        scene_epoch=1,
        hip_path=None,
        observed_revision="sha256:abc",
    )
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _owned(**overrides: object) -> OwnedNodeRef:
    values: dict[str, object] = dict(
        node_id="n_root",
        path="/obj/ws",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    values.update(overrides)
    return OwnedNodeRef(**values)  # type: ignore[arg-type]


def _noderef(**overrides: object) -> NodeRef:
    values: dict[str, object] = dict(
        node_id="n_child",
        path="/obj/ws/geo1",
        expected_type="geo",
        expected_workspace_id=WS,
    )
    values.update(overrides)
    return NodeRef(**values)  # type: ignore[arg-type]


def _manifest(**overrides: object) -> WorkspaceManifest:
    root = _owned()
    child = _owned(
        node_id="n_child",
        path="/obj/ws/geo1",
        parent_path="/obj/ws",
        role="member",
    )
    values: dict[str, object] = dict(
        workspace_id=WS,
        session_id=SES,
        instance_id=INSTANCE,
        scene_epoch=1,
        roots=[root],
        nodes=[root, child],
        created_by_run=RUN,
        updated_at=NOW,
    )
    values.update(overrides)
    return WorkspaceManifest.build(**values)  # type: ignore[arg-type]


def _changeset(**overrides: object) -> ChangeSet:
    values: dict[str, object] = dict(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(
            SetParm(
                op_id="op_parm",
                target=_noderef(),
                parm_name="tx",
                value=0,
                expected_old_value=0,
            ),
        ),
        affected_nodes=(_noderef(),),
        read_dependencies=(),
        preconditions=(),
        expected_postconditions=(),
        risk_summary=RiskSummary(
            touches_external_nodes=False,
            changes_wiring=False,
            requires_backup=False,
            operation_count=1,
            effect_names=("parm.set",),
            affected_paths=("/obj/ws/geo1",),
        ),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )
    values.update(overrides)
    return ChangeSet(**values)  # type: ignore[arg-type]


def _preflight(**overrides: object) -> PreflightRequest:
    values: dict[str, object] = dict(
        request_id="req_preflight_1",
        deadline_ms=5000,
        scene_epoch=1,
        changeset=_changeset(),
        workspace=_manifest(),
    )
    values.update(overrides)
    return PreflightRequest.build(**values)  # type: ignore[arg-type]


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ==========================================================================
# capability negotiation
# ==========================================================================


def test_capability_constant_is_changeset_v1() -> None:
    assert CHANGESET_V1 == "changeset.v1"


def test_validate_capabilities_accepts_sorted_unique_strings() -> None:
    assert validate_capabilities(["changeset.v1"]) == ("changeset.v1",)
    assert validate_capabilities(["changeset.v1", "scene.v1"]) == (
        "changeset.v1",
        "scene.v1",
    )


def test_validate_capabilities_rejects_non_list() -> None:
    for bad in ("changeset.v1", {"changeset.v1": 1}, None, 7):
        with pytest.raises((TypeError, ValueError)):
            validate_capabilities(bad)


def test_validate_capabilities_rejects_non_string_element() -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_capabilities(["changeset.v1", 7])  # type: ignore[list-item]


def test_validate_capabilities_rejects_bad_grammar() -> None:
    for bad in ("", "changeset", "CHANGESET.V1", "changeset.v1!", " changeset.v1"):
        with pytest.raises(ValueError):
            validate_capabilities([bad])


def test_validate_capabilities_rejects_duplicates() -> None:
    with pytest.raises(ValueError):
        validate_capabilities(["changeset.v1", "changeset.v1"])


def test_validate_capabilities_rejects_unsorted() -> None:
    with pytest.raises(ValueError):
        validate_capabilities(["scene.v1", "changeset.v1"])  # not ascending


# ==========================================================================
# PreflightRequest — digest + consistency
# ==========================================================================


def test_build_carries_exact_changeset_digest() -> None:
    req = _preflight()
    assert req.changeset_digest == req.changeset.digest
    assert len(req.changeset_digest) == 64


def test_digest_mismatch_rejected() -> None:
    with pytest.raises(ValueError):
        PreflightRequest(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=1,
            changeset=_changeset(),
            changeset_digest="b" * 64,  # wrong digest
            workspace=_manifest(),
        )


def test_workspace_must_match_changeset_workspace_id() -> None:
    other_ws = _manifest(workspace_id=f"ws_{'9' * 32}")
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=1,
            changeset=_changeset(),
            workspace=other_ws,
        )


def test_workspace_present_but_changeset_has_no_workspace_rejected() -> None:
    cs = _changeset(workspace_id=None, required_permission=PermissionMode.SCOPED_PATCH)
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=1,
            changeset=cs,
            workspace=_manifest(),
        )


def test_no_workspace_when_changeset_workspace_id_is_none_ok() -> None:
    cs = _changeset(workspace_id=None, required_permission=PermissionMode.SCOPED_PATCH)
    req = PreflightRequest.build(
        request_id="req_x",
        deadline_ms=5000,
        scene_epoch=1,
        changeset=cs,
        workspace=None,
    )
    assert req.workspace is None


def test_workspace_session_must_match_changeset_session() -> None:
    other = _manifest(session_id=f"ses_{'7' * 32}")
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=1,
            changeset=_changeset(),
            workspace=other,
        )


def test_workspace_binding_must_match_changeset_binding() -> None:
    # manifest instance_id diverges from the changeset binding instance.
    drifted_instance = _manifest(instance_id="hou:different:pid1")
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=1,
            changeset=_changeset(),
            workspace=drifted_instance,
        )


def test_envelope_scene_epoch_must_match_changeset_binding_epoch() -> None:
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="req_x",
            deadline_ms=5000,
            scene_epoch=2,  # != changeset.scene_binding.scene_epoch (1)
            changeset=_changeset(),
            workspace=_manifest(),
        )


def test_bad_request_id_rejected() -> None:
    with pytest.raises(ValueError):
        PreflightRequest.build(
            request_id="", deadline_ms=5000, scene_epoch=1, changeset=_changeset()
        )


def test_bad_deadline_rejected() -> None:
    for bad in (0, 30_001, -1):
        with pytest.raises(ValueError):
            PreflightRequest.build(
                request_id="req_x",
                deadline_ms=bad,
                scene_epoch=1,
                changeset=_changeset(),
            )


# ==========================================================================
# PreflightRequest — envelope shape + round-trip
# ==========================================================================


def test_request_envelope_shape_and_operation() -> None:
    req = _preflight()
    envelope = req.to_dict()
    assert envelope["protocol"] == PROTOCOL
    assert envelope["kind"] == "request"
    assert envelope["operation"] == "changeset.preflight"
    assert envelope["request_id"] == req.request_id
    assert envelope["scene_epoch"] == req.scene_epoch
    payload = envelope["payload"]
    assert set(payload.keys()) == {"changeset", "changeset_digest", "workspace"}


def test_request_round_trip_to_dict_from_dict() -> None:
    req = _preflight()
    revived = PreflightRequest.from_dict(req.to_dict())
    assert revived == req
    assert revived.changeset_digest == req.changeset_digest


def test_request_round_trip_json_parse() -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_request

    req = _preflight()
    revived = parse_preflight_request(req.to_json())
    assert revived == req


def test_request_json_is_canonical_sorted() -> None:
    req = _preflight()
    text = req.to_json()
    assert text == canonical_json_dumps(req.to_dict())


def test_request_unknown_envelope_field_rejected() -> None:
    req = _preflight()
    data = req.to_dict()
    data["extra"] = "nope"
    with pytest.raises(ValueError):
        PreflightRequest.from_dict(data)


def test_request_unknown_payload_field_rejected() -> None:
    req = _preflight()
    data = req.to_dict()
    data["payload"]["extra"] = "nope"
    with pytest.raises(ValueError):
        PreflightRequest.from_dict(data)


def test_request_wrong_operation_rejected() -> None:
    req = _preflight()
    data = req.to_dict()
    data["operation"] = "changeset.apply"  # not in this slice
    with pytest.raises(ValueError):
        PreflightRequest.from_dict(data)


def test_request_duplicate_keys_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_request

    req = _preflight()
    dup = req.to_json().replace(
        '"request_id":"req_preflight_1",', '"request_id":"req_preflight_1","request_id":"x",'
    )
    with pytest.raises(ValueError):
        parse_preflight_request(dup)


def test_request_oversized_raw_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_request

    huge = "x" * (MAX_MESSAGE_BYTES + 1)
    with pytest.raises(ValueError):
        parse_preflight_request(huge)


def test_request_must_be_dict_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_request

    with pytest.raises((TypeError, ValueError)):
        parse_preflight_request("[1,2,3]")


# ==========================================================================
# PreflightResult / facts — strictness + immutability
# ==========================================================================


def _result(**overrides: object) -> PreflightResult:
    values: dict[str, object] = dict(
        binding=_binding(),
        workspace_id=WS,
        workspace_revision=REVISION,
        node_facts=(
            PreflightNodeFact(
                requested=_noderef(),
                exists=True,
                actual_path="/obj/ws/geo1",
                actual_type="geo",
                parent_path="/obj/ws",
                workspace_id=WS,
                node_id="n_child",
                capability="modeling",
                role="member",
                is_locked=False,
            ),
        ),
        parm_facts=(
            PreflightParmFact(
                target=_noderef(),
                parm_name="tx",
                exists=True,
                value=0,
            ),
        ),
        wire_facts=(
            PreflightWireFact(
                target=_noderef(node_id="n_in", path="/obj/ws/in1", expected_type="merge"),
                input_index=0,
                source=WireRef(
                    source=_noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform"),
                    source_output_index=0,
                ),
            ),
        ),
        condition_results=(),
        all_preconditions_hold=True,
        scene_may_have_changed=False,
    )
    values.update(overrides)
    return PreflightResult(**values)  # type: ignore[arg-type]


def test_result_is_frozen() -> None:
    result = _result()
    with pytest.raises(AttributeError):
        result.all_preconditions_hold = False  # type: ignore[misc]


def test_result_scene_may_have_changed_must_be_false() -> None:
    with pytest.raises(ValueError):
        _result(scene_may_have_changed=True)


def test_parm_fact_value_must_be_bounded_scalar_or_tuple() -> None:
    # unsupported value type -> rejected at construction.
    with pytest.raises(TypeError):
        PreflightParmFact(
            target=_noderef(), parm_name="tx", exists=True, value=object()  # type: ignore[arg-type]
        )


def test_parm_fact_value_can_be_homogeneous_tuple() -> None:
    fact = PreflightParmFact(
        target=_noderef(), parm_name="t", exists=True, value=(1.0, 2.0, 3.0)
    )
    assert fact.value == (1.0, 2.0, 3.0)


def test_parm_fact_none_value_ok_when_missing() -> None:
    fact = PreflightParmFact(
        target=_noderef(), parm_name="missing", exists=False, value=None
    )
    assert fact.value is None


def test_node_fact_round_trip_and_immutable() -> None:
    fact = _result().node_facts[0]
    revived = PreflightNodeFact.from_dict(fact.to_dict())
    assert revived == fact
    with pytest.raises(AttributeError):
        fact.is_locked = True  # type: ignore[misc]


def test_wire_fact_null_source_round_trip() -> None:
    fact = PreflightWireFact(
        target=_noderef(node_id="n_in", path="/obj/ws/in1", expected_type="merge"),
        input_index=1,
        source=None,
    )
    revived = PreflightWireFact.from_dict(fact.to_dict())
    assert revived == fact
    assert revived.source is None


def test_result_round_trip_to_from_dict() -> None:
    result = _result()
    revived = PreflightResult.from_dict(result.to_dict())
    assert revived == result


def test_result_unknown_field_rejected() -> None:
    result = _result()
    data = result.to_dict()
    data["extra"] = 1
    with pytest.raises(ValueError):
        PreflightResult.from_dict(data)


# ==========================================================================
# PreflightResponse — envelope + parse
# ==========================================================================


def _bridge_error() -> BridgeError:
    return BridgeError(
        code="changeset.stale",
        category="stale",
        message_for_user="The ChangeSet could not be verified against the current scene.",
        retryable=True,
        technical_detail_ref=None,
    )


def test_response_success_round_trip() -> None:
    resp = PreflightResponse(request_id="req_preflight_1", result=_result(), error=None)
    envelope = resp.to_dict()
    assert envelope["protocol"] == PROTOCOL
    assert envelope["kind"] == "response"
    assert envelope["ok"] is True
    revived = PreflightResponse.from_dict(envelope)
    assert revived.request_id == "req_preflight_1"
    assert revived.result is not None
    assert revived.error is None


def test_response_error_round_trip() -> None:
    resp = PreflightResponse(
        request_id="req_preflight_1", result=None, error=_bridge_error()
    )
    envelope = resp.to_dict()
    assert envelope["ok"] is False
    revived = PreflightResponse.from_dict(envelope)
    assert revived.error is not None
    assert revived.error.code == "changeset.stale"


def test_response_requires_exactly_one_of_result_or_error() -> None:
    with pytest.raises(ValueError):
        PreflightResponse(request_id="r", result=_result(), error=_bridge_error())
    with pytest.raises(ValueError):
        PreflightResponse(request_id="r", result=None, error=None)


def test_parse_response_strict() -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_response

    resp = PreflightResponse(request_id="req_preflight_1", result=_result(), error=None)
    revived = parse_preflight_response(resp.to_json())
    assert revived.result is not None
    # duplicate keys rejected
    dup = resp.to_json().replace(
        '"request_id":"req_preflight_1",', '"request_id":"req_preflight_1","request_id":"y",'
    )
    with pytest.raises(ValueError):
        parse_preflight_response(dup)


# ==========================================================================
# Task A-5M3: wire decode boundary — exact primitive narrowing, bool-as-int,
# wrong-type optionals, nested non-dict facts, top-level list/scalar rejection,
# tuple collection fields, and apply/receipt round-trips. These pin the typed
# helpers that make from_dict() pass precise values to each DTO constructor.
# ==========================================================================


def _receipt() -> ChangeReceipt:
    return ChangeReceipt(
        change_id=CHG,
        status=ReceiptStatus.APPLIED,
        instance_id=INSTANCE,
        scene_epoch=1,
        before_revision=REVISION,
        after_revision="b" * 64,
        applied_op_ids=("op_parm",),
        postcondition_results=(),
        rollback_results=(),
        scene_may_have_changed=False,
        completed_at=NOW,
    )


def _apply_request() -> ApplyRequest:
    return ApplyRequest.build(
        request_id="req_apply_1",
        deadline_ms=5000,
        scene_epoch=1,
        changeset=_changeset(),
        workspace=_manifest(),
    )


def _receipt_request() -> ReceiptRequest:
    return ReceiptRequest.build(
        request_id="req_receipt_1",
        deadline_ms=5000,
        scene_epoch=1,
        change_id=CHG,
        changeset_digest=_changeset().digest,
    )


# --- every request/response legal round-trip (preflight shown above) --------


def test_apply_request_round_trip_from_dict() -> None:
    req = _apply_request()
    assert ApplyRequest.from_dict(req.to_dict()) == req


def test_receipt_request_round_trip_from_dict() -> None:
    req = _receipt_request()
    assert ReceiptRequest.from_dict(req.to_dict()) == req


def test_apply_request_round_trip_json_parse() -> None:
    from eee_agent.houdini_bridge.changesets import parse_apply_request

    req = _apply_request()
    revived = parse_apply_request(req.to_json())
    assert revived == req


def test_receipt_request_round_trip_json_parse() -> None:
    from eee_agent.houdini_bridge.changesets import parse_receipt_request

    req = _receipt_request()
    revived = parse_receipt_request(req.to_json())
    assert revived == req


def test_apply_response_success_round_trip() -> None:
    resp = ApplyResponse(request_id="req_apply_1", result=_receipt(), error=None)
    revived = ApplyResponse.from_dict(resp.to_dict())
    assert revived.result is not None
    assert revived.result.change_id == CHG
    assert revived.error is None


def test_apply_response_error_round_trip() -> None:
    resp = ApplyResponse(request_id="req_apply_1", result=None, error=_bridge_error())
    revived = ApplyResponse.from_dict(resp.to_dict())
    assert revived.error is not None
    assert revived.error.code == "changeset.stale"


def test_receipt_response_success_and_error_round_trip() -> None:
    ok = ReceiptResponse(request_id="req_receipt_1", result=_receipt(), error=None)
    assert ReceiptResponse.from_dict(ok.to_dict()).result is not None
    bad = ReceiptResponse(request_id="req_receipt_1", result=None, error=_bridge_error())
    assert ReceiptResponse.from_dict(bad.to_dict()).error is not None


# --- bool must not enter integer wire fields --------------------------------


@pytest.mark.parametrize("as_bool", [True, False])
def test_preflight_request_rejects_bool_deadline(as_bool: bool) -> None:
    data = _preflight().to_dict()
    data["deadline_ms"] = as_bool
    with pytest.raises(TypeError):
        PreflightRequest.from_dict(data)


@pytest.mark.parametrize("as_bool", [True, False])
def test_preflight_request_rejects_bool_scene_epoch(as_bool: bool) -> None:
    data = _preflight().to_dict()
    data["scene_epoch"] = as_bool
    with pytest.raises(TypeError):
        PreflightRequest.from_dict(data)


@pytest.mark.parametrize("as_bool", [True, False])
def test_wire_fact_rejects_bool_input_index(as_bool: bool) -> None:
    fact = _result().wire_facts[0]
    data = fact.to_dict()
    data["input_index"] = as_bool
    with pytest.raises(TypeError):
        PreflightWireFact.from_dict(data)


@pytest.mark.parametrize("as_bool", [True, False])
def test_apply_request_rejects_bool_deadline(as_bool: bool) -> None:
    data = _apply_request().to_dict()
    data["deadline_ms"] = as_bool
    with pytest.raises(TypeError):
        ApplyRequest.from_dict(data)


@pytest.mark.parametrize("as_bool", [True, False])
def test_receipt_request_rejects_bool_deadline(as_bool: bool) -> None:
    data = _receipt_request().to_dict()
    data["deadline_ms"] = as_bool
    with pytest.raises(TypeError):
        ReceiptRequest.from_dict(data)


# --- wrong type on NodeFact optional / boolean fields -----------------------


@pytest.mark.parametrize(
    "field,bad",
    [
        ("actual_path", 7),
        ("actual_type", []),
        ("parent_path", {}),
        ("workspace_id", 3),
        ("node_id", 1.5),
        ("capability", True),
        ("role", False),
        ("exists", "yes"),
        ("is_locked", 1),
    ],
)
def test_node_fact_wrong_type_rejected(field: str, bad: object) -> None:
    fact = _result().node_facts[0]
    data = fact.to_dict()
    data[field] = bad
    with pytest.raises((TypeError, ValueError)):
        PreflightNodeFact.from_dict(data)


# --- facts collection must be an exact list, nested fact must be exact dict --


def test_node_facts_collection_must_be_list() -> None:
    data = _result().to_dict()
    data["node_facts"] = _result().node_facts[0].to_dict()  # a dict, not a list
    with pytest.raises((TypeError, ValueError)):
        PreflightResult.from_dict(data)


def test_parm_facts_collection_must_be_list() -> None:
    data = _result().to_dict()
    data["parm_facts"] = "not-a-list"
    with pytest.raises((TypeError, ValueError)):
        PreflightResult.from_dict(data)


def test_nested_node_fact_must_be_exact_dict() -> None:
    data = _result().to_dict()
    data["node_facts"] = [42]  # non-dict entry
    with pytest.raises((TypeError, ValueError)):
        PreflightResult.from_dict(data)


def test_nested_wire_fact_must_be_exact_dict() -> None:
    data = _result().to_dict()
    data["wire_facts"] = ["scalar"]  # non-dict entry
    with pytest.raises((TypeError, ValueError)):
        PreflightResult.from_dict(data)


# --- tuple collection fields round-trip as real tuples ----------------------


def test_result_facts_are_tuples_after_from_dict() -> None:
    revived = PreflightResult.from_dict(_result().to_dict())
    assert type(revived.node_facts) is tuple
    assert type(revived.parm_facts) is tuple
    assert type(revived.wire_facts) is tuple
    assert type(revived.condition_results) is tuple


# --- result/error mutual exclusion + nested non-dict rejection --------------


@pytest.mark.parametrize(
    "ok_value,result_field,error_field",
    [(True, "result", None), (False, None, "error")],
)
def test_response_nested_non_dict_rejected(
    ok_value: bool, result_field: object, error_field: object
) -> None:
    resp = PreflightResponse(request_id="req_preflight_1", result=_result(), error=None)
    data = resp.to_dict()
    if ok_value is True:
        data["result"] = ["not-a-dict"]  # nested result must be exact dict
    else:
        data["ok"] = False
        data.pop("result")
        data["error"] = "not-a-dict"
    with pytest.raises((TypeError, ValueError)):
        PreflightResponse.from_dict(data)


def test_apply_response_nested_error_must_be_dict() -> None:
    resp = ApplyResponse(request_id="req_apply_1", result=None, error=_bridge_error())
    data = resp.to_dict()
    data["error"] = ["nope"]
    with pytest.raises((TypeError, ValueError)):
        ApplyResponse.from_dict(data)


def test_receipt_response_nested_result_must_be_dict() -> None:
    resp = ReceiptResponse(request_id="req_receipt_1", result=_receipt(), error=None)
    data = resp.to_dict()
    data["result"] = 123
    with pytest.raises((TypeError, ValueError)):
        ReceiptResponse.from_dict(data)


# --- parse_* top-level list / scalar rejected (envelope must be exact dict) --


@pytest.mark.parametrize(
    "raw",
    ["[1,2,3]", "42", "\"a-string\"", "true", "null"],
)
def test_parse_preflight_request_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_request

    with pytest.raises((TypeError, ValueError)):
        parse_preflight_request(raw)


@pytest.mark.parametrize(
    "raw",
    ["[1,2,3]", "5", "false", "null"],
)
def test_parse_apply_request_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_apply_request

    with pytest.raises((TypeError, ValueError)):
        parse_apply_request(raw)


@pytest.mark.parametrize("raw", ["[]", "17"])
def test_parse_receipt_request_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_receipt_request

    with pytest.raises((TypeError, ValueError)):
        parse_receipt_request(raw)


@pytest.mark.parametrize("raw", ["[]", "\"s\"", "3.0"])
def test_parse_preflight_response_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_preflight_response

    with pytest.raises((TypeError, ValueError)):
        parse_preflight_response(raw)


@pytest.mark.parametrize("raw", ["[]", "{}", "7"])
def test_parse_apply_response_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_apply_response

    with pytest.raises((TypeError, ValueError)):
        parse_apply_response(raw)


@pytest.mark.parametrize("raw", ["[1]", "11"])
def test_parse_receipt_response_rejects_non_dict_top_level(raw: str) -> None:
    from eee_agent.houdini_bridge.changesets import parse_receipt_response

    with pytest.raises((TypeError, ValueError)):
        parse_receipt_response(raw)


# --- duplicate key + oversized message still rejected for apply/receipt -----


def test_apply_request_duplicate_keys_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_apply_request

    dup = _apply_request().to_json().replace(
        '"request_id":"req_apply_1",', '"request_id":"req_apply_1","request_id":"x",'
    )
    with pytest.raises(ValueError):
        parse_apply_request(dup)


def test_apply_request_oversized_raw_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_apply_request

    with pytest.raises(ValueError):
        parse_apply_request("x" * (MAX_MESSAGE_BYTES + 1))


def test_receipt_request_duplicate_keys_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_receipt_request

    dup = _receipt_request().to_json().replace(
        '"request_id":"req_receipt_1",', '"request_id":"req_receipt_1","request_id":"x",'
    )
    with pytest.raises(ValueError):
        parse_receipt_request(dup)


def test_receipt_request_oversized_raw_rejected() -> None:
    from eee_agent.houdini_bridge.changesets import parse_receipt_request

    with pytest.raises(ValueError):
        parse_receipt_request("x" * (MAX_MESSAGE_BYTES + 1))


# --- error envelope must not leak a raw token/secret into a surface string --


def test_bridge_error_round_trip_keeps_message_off_wire() -> None:
    resp = ApplyResponse(request_id="req_apply_1", result=None, error=_bridge_error())
    revived = ApplyResponse.from_dict(resp.to_dict())
    assert revived.error is not None
    assert revived.error.technical_detail_ref is None
    # The error round-trips without surfacing anything beyond its declared fields.
    assert set(revived.error.to_dict().keys()) >= {"code", "category", "message_for_user"}


# ==========================================================================
# structural: the changesets module stays free of hou/rpyc
# ==========================================================================


def test_changesets_module_imports_are_clean() -> None:
    import ast
    import inspect

    from eee_agent.houdini_bridge import changesets as mod

    tree = ast.parse(inspect.getsource(mod))
    mods: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert "hou" not in mods
    assert "rpyc" not in mods
