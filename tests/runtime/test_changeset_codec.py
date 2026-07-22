"""Contract tests for the shared ChangeSet/Manifest/Receipt codec (Stage A-5M2).

These tests lock the new shared decode boundary introduced in
``eee_agent.changesets.codec``:

* golden DTO round-trips (``to_dict()`` -> ``decode_*`` -> ``to_dict()``);
* the repository private wrappers and the Bridge private wrappers both
  delegate to the same codec, so a given payload decodes to identical DTOs;
* collection fields are stored as ``tuple`` (never ``list``);
* the malformed-payload matrix (unknown field, missing field, wrong primitive
  type, bool-as-int, nested non-dict) is rejected with ``TypeError``/
  ``ValueError`` at the codec boundary;
* no raw mapping leaks into a typed DTO field.

The codec is pure-contract: it imports only the DTO contracts and
``SceneBinding``. These tests assert that property indirectly by exercising the
codec directly plus the two consumer modules' private wrappers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime

import pytest

from eee_agent.changesets import codec
from eee_agent.changesets.contracts import (
    ApprovalDecision,
    ApprovalRecord,
    ChangeReceipt,
    ChangeSet,
    CheckpointPlan,
    ConditionResult,
    ConnectInput,
    CreateNode,
    NodeRef,
    OwnedNodeRef,
    ParmSnapshot,
    ParmValueEquals,
    PermissionMode,
    ReceiptStatus,
    RiskSummary,
    SetParm,
    WireRef,
    WireSnapshot,
    WorkspaceManifest,
)
from eee_agent.houdini_bridge import changesets as bridge
from eee_agent.houdini_bridge.contracts import SceneBinding

# Importing repository pulls eee_agent.runtime.database + sqlite; that is fine
# for the test environment and lets us assert the repo wrappers agree.
from eee_agent.changesets import repository as repo

NOW_ISO = "2026-07-22T10:00:00+00:00"
LATER_ISO = "2026-07-22T10:05:00+00:00"
SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
CHG = f"chg_{'4' * 32}"
APR = f"apr_{'5' * 32}"
REVISION = "a" * 64
AFTER_REV = "b" * 64


# --------------------------------------------------------------------------
# golden DTO builders (self-contained; mirror the contract test fixtures)
# --------------------------------------------------------------------------


def _owned(
    *,
    node_id: str = "n_root",
    path: str = "/obj/ws",
    node_type: str = "geo",
    parent_path: str = "/obj",
    capability: str = "modeling",
    role: str = "root",
) -> OwnedNodeRef:
    return OwnedNodeRef(
        node_id=node_id,
        path=path,
        node_type=node_type,
        parent_path=parent_path,
        capability=capability,
        role=role,
    )


def _noderef(
    *,
    node_id: str | None = "n_child",
    path: str = "/obj/ws/geo1",
    expected_type: str = "geo",
    expected_workspace_id: str | None = WS,
) -> NodeRef:
    return NodeRef(
        node_id=node_id,
        path=path,
        expected_type=expected_type,
        expected_workspace_id=expected_workspace_id,
    )


def _wiref() -> WireRef:
    return WireRef(
        source=_noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform"),
        source_output_index=0,
    )


def _create() -> CreateNode:
    return CreateNode(
        op_id="op_create",
        parent=_noderef(node_id="n_root", path="/obj/ws", expected_type="subnet"),
        node_id="n_new",
        node_type="geo",
        node_name="geo_new",
        workspace_id=WS,
        capability="modeling",
        role="member",
    )


def _setparm() -> SetParm:
    return SetParm(
        op_id="op_parm",
        target=_noderef(),
        parm_name="tx",
        value=0,
        expected_old_value=0,
    )


def _connect() -> ConnectInput:
    return ConnectInput(
        op_id="op_wire",
        target=_noderef(node_id="n_in", path="/obj/ws/in1", expected_type="merge"),
        input_index=0,
        source=_noderef(node_id="n_src", path="/obj/ws/src1", expected_type="xform"),
        source_output_index=0,
        expected_old_source=None,
    )


def _manifest() -> WorkspaceManifest:
    root = _owned()
    child = _owned(
        node_id="n_child_0", path="/obj/ws/c0", parent_path="/obj/ws", role="member"
    )
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=(root,),
        nodes=(root, child),
        created_by_run=RUN,
        updated_at=_dt(NOW_ISO),
    )


def _risk() -> RiskSummary:
    return RiskSummary(
        touches_external_nodes=False,
        changes_wiring=False,
        requires_backup=False,
        operation_count=1,
        effect_names=("node.create",),
        affected_paths=("/obj/ws/geo_new",),
    )


def _checkpoint() -> CheckpointPlan:
    return CheckpointPlan(
        nodes=(_noderef(),),
        parameters=(ParmSnapshot(target=_noderef(), parm_name="tx"),),
        wires=(WireSnapshot(target=_noderef(), input_index=0),),
    )


def _binding_dict() -> dict[str, object]:
    return {
        "instance_id": "hou_instance_1",
        "scene_epoch": 1,
        "hip_path": None,
        "observed_revision": REVISION,
    }


def _changeset() -> ChangeSet:
    return ChangeSet(
        change_id=CHG,
        session_id=SES,
        run_id=RUN,
        scene_binding=_binding(),
        workspace_id=WS,
        base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE,
        scoped_node_ids=(),
        operations=(_create(),),
        affected_nodes=(_noderef(node_id="n_new", path="/obj/ws/geo_new"),),
        read_dependencies=(),
        preconditions=(),
        expected_postconditions=(
            ParmValueEquals(
                target=_noderef(node_id="n_new", path="/obj/ws/geo_new"),
                parm_name="tx",
                value=0,
            ),
        ),
        risk_summary=_risk(),
        checkpoint_plan=_checkpoint(),
        created_at=_dt(NOW_ISO),
    )


def _approval() -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=APR,
        change_id=CHG,
        changeset_digest=REVISION,
        decision=ApprovalDecision.PENDING,
        decided_by=None,
        requested_at=_dt(NOW_ISO),
        decided_at=None,
        expires_at=_dt(LATER_ISO),
        approved_instance_id=None,
        approved_scene_epoch=None,
    )


def _receipt() -> ChangeReceipt:
    return ChangeReceipt(
        change_id=CHG,
        status=ReceiptStatus.APPLIED,
        instance_id="hou_instance_1",
        scene_epoch=1,
        before_revision=REVISION,
        after_revision=AFTER_REV,
        applied_op_ids=("op_create",),
        postcondition_results=(ConditionResult(kind="parm.value_equals", passed=True),),
        rollback_results=(),
        scene_may_have_changed=False,
        completed_at=_dt(LATER_ISO),
    )


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _binding() -> SceneBinding:
    # Build a real SceneBinding for the ChangeSet field.
    return SceneBinding(
        instance_id="hou_instance_1",
        scene_epoch=1,
        hip_path=None,
        observed_revision=REVISION,
    )


# --------------------------------------------------------------------------
# 1. golden round-trip via the codec
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dto", "decode"),
    [
        (_owned(), codec.decode_owned_node_ref),
        (_noderef(), codec.decode_node_ref),
        (_wiref(), codec.decode_wire_ref),
        (_create(), codec.decode_operation),
        (_setparm(), codec.decode_operation),
        (_connect(), codec.decode_operation),
        (_risk(), codec.decode_risk_summary),
        (_checkpoint(), codec.decode_checkpoint_plan),
        (_manifest(), codec.decode_workspace_manifest),
        (_changeset(), codec.decode_changeset),
        (_approval(), codec.decode_approval_record),
        (_receipt(), codec.decode_change_receipt),
    ],
)
def test_codec_round_trips_golden_dto(dto, decode) -> None:
    encoded = dto.to_dict()
    decoded = decode(encoded)
    assert type(decoded) is type(dto)
    assert decoded.to_dict() == encoded


def test_codec_changeset_digest_stable_across_round_trip() -> None:
    cs = _changeset()
    assert codec.decode_changeset(cs.to_dict()).digest == cs.digest


def test_codec_manifest_roots_and_nodes_are_tuples() -> None:
    decoded = codec.decode_workspace_manifest(_manifest().to_dict())
    assert type(decoded.roots) is tuple
    assert type(decoded.nodes) is tuple
    assert all(type(r) is OwnedNodeRef for r in decoded.roots)


def test_codec_changeset_collections_are_tuples_and_order_preserved() -> None:
    decoded = codec.decode_changeset(_changeset().to_dict())
    assert type(decoded.operations) is tuple
    assert type(decoded.affected_nodes) is tuple
    assert type(decoded.preconditions) is tuple
    assert type(decoded.expected_postconditions) is tuple
    assert decoded.operations[0].op_id == "op_create"


def test_codec_receipt_collections_are_tuples() -> None:
    decoded = codec.decode_change_receipt(_receipt().to_dict())
    assert type(decoded.applied_op_ids) is tuple
    assert type(decoded.postcondition_results) is tuple
    assert type(decoded.rollback_results) is tuple


def test_codec_risk_summary_collections_are_tuples() -> None:
    decoded = codec.decode_risk_summary(_risk().to_dict())
    assert type(decoded.effect_names) is tuple
    assert type(decoded.affected_paths) is tuple


# --------------------------------------------------------------------------
# 2. differential decode: repository wrappers == bridge wrappers == codec
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload_builder", "repo_decode", "bridge_decode", "codec_decode"),
    [
        (lambda: _owned().to_dict(), repo._decode_owned, bridge._decode_owned,
         codec.decode_owned_node_ref),
        (lambda: _noderef().to_dict(), repo._decode_noderef, bridge._decode_noderef,
         codec.decode_node_ref),
        (lambda: _create().to_dict(), repo._decode_operation, bridge._decode_operation,
         codec.decode_operation),
        (lambda: _manifest().to_dict(), repo._decode_manifest, bridge._decode_manifest,
         codec.decode_workspace_manifest),
        (lambda: _changeset().to_dict(), repo._decode_changeset, bridge._decode_changeset,
         codec.decode_changeset),
        (lambda: _receipt().to_dict(), repo._decode_receipt, bridge._decode_receipt,
         codec.decode_change_receipt),
    ],
)
def test_repository_and_bridge_decoders_match_codec(
    payload_builder, repo_decode, bridge_decode, codec_decode
) -> None:
    payload = payload_builder()
    via_repo = repo_decode(payload).to_dict()
    via_bridge = bridge_decode(payload).to_dict()
    via_codec = codec_decode(payload).to_dict()
    assert via_repo == via_codec
    assert via_bridge == via_codec


def test_decode_change_receipt_public_alias_matches_private() -> None:
    payload = _receipt().to_dict()
    assert bridge.decode_change_receipt(payload).to_dict() == (
        codec.decode_change_receipt(payload).to_dict()
    )


# --------------------------------------------------------------------------
# 3. malformed payload matrix
# --------------------------------------------------------------------------


def test_codec_rejects_unknown_field() -> None:
    payload = _owned().to_dict()
    payload["unexpected"] = "x"
    with pytest.raises(ValueError, match="required fields"):
        codec.decode_owned_node_ref(payload)


def test_codec_rejects_missing_field() -> None:
    payload = _owned().to_dict()
    del payload["role"]
    with pytest.raises(ValueError, match="required fields"):
        codec.decode_owned_node_ref(payload)


def test_codec_rejects_wrong_primitive_type() -> None:
    payload = _owned().to_dict()
    payload["node_id"] = 42
    with pytest.raises(TypeError, match="node_id"):
        codec.decode_owned_node_ref(payload)


def test_codec_rejects_bool_where_int_required() -> None:
    payload = _wiref().to_dict()
    payload["source_output_index"] = True
    with pytest.raises(TypeError, match="integer"):
        codec.decode_wire_ref(payload)


def test_codec_rejects_nested_non_dict() -> None:
    payload = _wiref().to_dict()
    payload["source"] = "not-a-dict"
    with pytest.raises(TypeError, match="exact dict"):
        codec.decode_wire_ref(payload)


def test_codec_rejects_non_dict_top_level() -> None:
    for bad in (None, "x", 1, [], 1.5):
        with pytest.raises(TypeError, match="exact dict"):
            codec.decode_owned_node_ref(bad)


def test_codec_rejects_non_list_for_collection_field() -> None:
    payload = _manifest().to_dict()
    payload["roots"] = {"node_id": "x"}  # a dict where a list is required
    with pytest.raises(TypeError, match="exact list"):
        codec.decode_workspace_manifest(payload)


def test_codec_rejects_unknown_operation_kind() -> None:
    payload = _create().to_dict()
    payload["kind"] = "node.explode"
    with pytest.raises(ValueError, match="unsupported operation kind tag"):
        codec.decode_operation(payload)


def test_codec_rejects_non_postcondition_in_expected_postconditions() -> None:
    payload = _changeset().to_dict()
    # scene.binding_equals is a valid Precondition but not a Postcondition.
    payload["expected_postconditions"] = [
        {"kind": "scene.binding_equals", "instance_id": "hou_instance_1",
         "scene_epoch": 1}
    ]
    with pytest.raises(TypeError, match="expected_postconditions"):
        codec.decode_changeset(payload)


def test_codec_rejects_bool_in_risk_int_count() -> None:
    payload = _risk().to_dict()
    payload["operation_count"] = True
    with pytest.raises(TypeError, match="integer"):
        codec.decode_risk_summary(payload)


# --------------------------------------------------------------------------
# 4. no raw mapping leaks: decoded DTOs are exact frozen types, not dicts
# --------------------------------------------------------------------------


def test_codec_returns_exact_dto_types_not_mappings() -> None:
    assert type(codec.decode_owned_node_ref(_owned().to_dict())) is OwnedNodeRef
    assert type(codec.decode_node_ref(_noderef().to_dict())) is NodeRef
    assert type(codec.decode_risk_summary(_risk().to_dict())) is RiskSummary
    assert type(codec.decode_workspace_manifest(_manifest().to_dict())) is WorkspaceManifest
    assert type(codec.decode_changeset(_changeset().to_dict())) is ChangeSet
    assert type(codec.decode_change_receipt(_receipt().to_dict())) is ChangeReceipt
    assert type(codec.decode_approval_record(_approval().to_dict())) is ApprovalRecord


def test_codec_returns_typed_node_ref_not_mapping() -> None:
    # A decoded NodeRef must expose typed attributes, not behave like a dict.
    decoded = codec.decode_node_ref(_noderef().to_dict())
    assert not isinstance(decoded, Mapping)
    assert decoded.path == "/obj/ws/geo1"


# --------------------------------------------------------------------------
# 5. expected_postconditions regression: all three decode paths raise TypeError
#    for the same illegal payload (codec, repository wrapper, bridge wrapper)
# --------------------------------------------------------------------------


def _illegal_postconditions_payload() -> dict[str, object]:
    payload = _changeset().to_dict()
    # scene.binding_equals is a valid Precondition but not a Postcondition.
    payload["expected_postconditions"] = [
        {"kind": "scene.binding_equals", "instance_id": "hou_instance_1",
         "scene_epoch": 1}
    ]
    return payload


@pytest.mark.parametrize(
    ("label", "decode"),
    [
        ("codec", codec.decode_changeset),
        ("repository", repo._decode_changeset),
        ("bridge", bridge._decode_changeset),
    ],
)
def test_decode_changeset_rejects_non_postcondition_on_all_paths(
    label: str, decode
) -> None:
    payload = _illegal_postconditions_payload()
    with pytest.raises(TypeError, match="expected_postconditions") as exc_info:
        decode(payload)
    # The raw payload dict must never appear verbatim in the error message.
    assert repr(payload) not in str(exc_info.value)
    # label is asserted only to keep the parametrize id explicit; no behavior
    # difference is required across the three delegating paths.
    assert label in {"codec", "repository", "bridge"}


# --------------------------------------------------------------------------
# 6. repository._decode_row behavior: a minimal string-keyed fake row
#    (sqlite3.Row is not a dict and not a Mapping) drives digest verification
#    and DTO decode; non-str payload/digest cells raise TypeError.
# --------------------------------------------------------------------------


class _FakeRow:
    """A row that supports only ``row[key]`` for string keys (like sqlite3.Row).

    Deliberately *not* a ``dict`` and *not* a ``Mapping``: it has no
    ``keys``/``items``/``__iter__``, so it models the narrow capability the
    ``_StringKeyedRow`` Protocol requires and proves ``_decode_row`` needs no
    dict cast.
    """

    def __init__(self, cells: dict[str, object]) -> None:
        self._cells = cells

    def __getitem__(self, key: str) -> object:
        return self._cells[key]


def _row_receipt_pair() -> tuple[str, str]:
    payload_json = json.dumps(_receipt().to_dict())
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return payload_json, digest


def test_decode_row_decodes_valid_fake_row() -> None:
    payload_json, digest = _row_receipt_pair()
    row = _FakeRow({"payload_json": payload_json, "digest": digest})
    receipt = repo._decode_row(row, "payload_json", "digest", repo._decode_receipt)
    assert type(receipt) is ChangeReceipt
    assert receipt.change_id == CHG


def test_decode_row_rejects_non_str_payload_cell() -> None:
    _, digest = _row_receipt_pair()
    row = _FakeRow({"payload_json": 12345, "digest": digest})
    with pytest.raises(TypeError, match="payload_json"):
        repo._decode_row(row, "payload_json", "digest", repo._decode_receipt)


def test_decode_row_rejects_non_str_digest_cell() -> None:
    payload_json, _ = _row_receipt_pair()
    row = _FakeRow({"payload_json": payload_json, "digest": 67890})
    with pytest.raises(TypeError, match="digest"):
        repo._decode_row(row, "payload_json", "digest", repo._decode_receipt)
