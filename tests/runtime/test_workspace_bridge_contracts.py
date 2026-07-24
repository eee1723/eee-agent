"""Strict ``workspace.v1`` Bridge DTO contract tests (Task 16-B2b-1)."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    SceneBinding,
)
from eee_agent.houdini_bridge.workspaces import (
    WORKSPACE_INSPECT_OPERATION,
    WORKSPACE_V1,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceInspectResult,
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
    WorkspaceNodeObservation,
    parse_workspace_inspect_request,
    parse_workspace_inspect_response,
)
from eee_agent.runtime.models import canonical_json_dumps

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = {
        "instance_id": "hou:21.0.440:pid123",
        "scene_epoch": 3,
        "hip_path": None,
        "observed_revision": "sha256:scene",
    }
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _owned(**overrides: object) -> OwnedNodeRef:
    values: dict[str, object] = {
        "node_id": "node_root",
        "path": "/obj/eee/root",
        "node_type": "geo",
        "parent_path": "/obj/eee",
        "capability": "modeling",
        "role": "root",
    }
    values.update(overrides)
    return OwnedNodeRef(**values)  # type: ignore[arg-type]


def _manifest(**overrides: object) -> WorkspaceManifest:
    root = _owned()
    child = _owned(
        node_id="node_child",
        path="/obj/eee/root/box1",
        node_type="box",
        parent_path="/obj/eee/root",
        role="member",
    )
    values: dict[str, object] = {
        "workspace_id": WS,
        "session_id": SES,
        "instance_id": "hou:21.0.440:pid123",
        "scene_epoch": 3,
        "roots": (root,),
        "nodes": (root, child),
        "created_by_run": RUN,
        "updated_at": NOW,
    }
    values.update(overrides)
    return WorkspaceManifest.build(**values)  # type: ignore[arg-type]


def _observation(**overrides: object) -> WorkspaceNodeObservation:
    values: dict[str, object] = {
        "path": "/obj/eee/root",
        "node_type": "geo",
        "parent_path": "/obj/eee",
        "is_locked": False,
        "workspace_id": WS,
        "node_id": "node_root",
        "capability": "modeling",
        "role": "root",
        "schema_version": 1,
        "created_by_run": RUN,
    }
    values.update(overrides)
    return WorkspaceNodeObservation(**values)  # type: ignore[arg-type]


def _result(**overrides: object) -> WorkspaceInspectResult:
    observations = (
        _observation(node_id="node_child", path="/obj/eee/root/box1", node_type="box", parent_path="/obj/eee/root", role="member"),
        _observation(),
    )
    values: dict[str, object] = {
        "binding": _binding(),
        "mode": "selection",
        "observations": observations,
    }
    values.update(overrides)
    return WorkspaceInspectResult.build(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> WorkspaceInspectRequest:
    values: dict[str, object] = {
        "request_id": "req_workspace_1",
        "deadline_ms": 5000,
        "scene_epoch": 3,
        "mode": "selection",
        "manifest": None,
    }
    values.update(overrides)
    return WorkspaceInspectRequest(**values)  # type: ignore[arg-type]


def test_constants_and_marker_exceptions_are_exact() -> None:
    assert WORKSPACE_V1 == "workspace.v1"
    assert WORKSPACE_INSPECT_OPERATION == "workspace.inspect"
    assert issubclass(WorkspaceInspectionUnavailable, Exception)
    assert issubclass(WorkspaceInspectionConflict, Exception)


def test_workspace_symbols_are_available_from_lazy_package_surface() -> None:
    import eee_agent.houdini_bridge as bridge

    assert bridge.WORKSPACE_V1 == WORKSPACE_V1
    assert bridge.WorkspaceInspectRequest is WorkspaceInspectRequest
    assert bridge.WorkspaceNodeObservation is WorkspaceNodeObservation
    assert bridge.parse_workspace_inspect_response is parse_workspace_inspect_response


def test_request_selection_round_trip_is_strict_and_canonical() -> None:
    request = _request()
    assert request.to_dict() == {
        "protocol": PROTOCOL,
        "kind": "request",
        "request_id": "req_workspace_1",
        "operation": "workspace.inspect",
        "deadline_ms": 5000,
        "scene_epoch": 3,
        "payload": {"mode": "selection", "manifest": None},
    }
    assert request.to_json() == canonical_json_dumps(request.to_dict())
    assert parse_workspace_inspect_request(request.to_json()) == request


def test_request_manifest_round_trip_reconstructs_exact_manifest() -> None:
    request = _request(mode="manifest", manifest=_manifest())
    revived = parse_workspace_inspect_request(request.to_json())
    assert revived == request
    assert revived.manifest == _manifest()


@pytest.mark.parametrize(
    ("mode", "manifest"),
    [("selection", _manifest()), ("manifest", None), ("other", None)],
)
def test_request_mode_and_manifest_must_match(mode: str, manifest: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(mode=mode, manifest=manifest)


@pytest.mark.parametrize("scene_epoch", [True, 0, -1, "3"])
def test_request_rejects_invalid_nullable_scene_epoch(scene_epoch: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _request(scene_epoch=scene_epoch)
    assert _request(scene_epoch=None).scene_epoch is None


def test_request_rejects_unknown_fields_duplicate_keys_and_oversize() -> None:
    data = _request().to_dict()
    data["extra"] = 1
    with pytest.raises(ValueError):
        WorkspaceInspectRequest.from_dict(data)

    duplicate = _request().to_json().replace(
        '"request_id":"req_workspace_1",',
        '"request_id":"req_workspace_1","request_id":"other",',
    )
    with pytest.raises(ValueError):
        parse_workspace_inspect_request(duplicate)
    with pytest.raises(ValueError):
        parse_workspace_inspect_request("x" * (MAX_MESSAGE_BYTES + 1))


def test_request_rejects_wrong_protocol_kind_operation_and_payload_fields() -> None:
    for field, value in (
        ("protocol", "eee.bridge/2"),
        ("kind", "response"),
        ("operation", "scene.query"),
    ):
        data = _request().to_dict()
        data[field] = value
        with pytest.raises(ValueError):
            WorkspaceInspectRequest.from_dict(data)
    data = _request().to_dict()
    data["payload"]["paths"] = []
    with pytest.raises(ValueError):
        WorkspaceInspectRequest.from_dict(data)


def test_observation_accepts_partial_mirrors_but_is_frozen() -> None:
    observation = _observation(
        workspace_id=None,
        node_id=None,
        capability=None,
        role=None,
        schema_version=None,
        created_by_run=None,
    )
    assert observation.workspace_id is None
    with pytest.raises((FrozenInstanceError, AttributeError)):
        observation.path = "/obj/changed"  # type: ignore[misc]
    assert WorkspaceNodeObservation.from_dict(observation.to_dict()) == observation


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", "relative"),
        ("node_type", ""),
        ("parent_path", "relative"),
        ("is_locked", 0),
        ("workspace_id", "bad"),
        ("node_id", "bad-id"),
        ("schema_version", True),
        ("created_by_run", "run_bad"),
    ],
)
def test_observation_rejects_malformed_bounded_facts(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _observation(**{field: value})


def test_result_sorts_observations_and_computes_verified_revision() -> None:
    result = _result()
    assert tuple(item.node_id for item in result.observations) == (
        "node_child",
        "node_root",
    )
    assert len(result.observed_revision) == 64
    assert result == WorkspaceInspectResult.from_dict(result.to_dict())


def test_result_rejects_duplicate_ids_paths_bad_revision_and_uncertain_scene() -> None:
    with pytest.raises(ValueError):
        _result(observations=(_observation(), _observation(path="/obj/other")))
    with pytest.raises(ValueError):
        _result(observations=(_observation(), _observation(node_id="node_other")))
    valid = _result()
    with pytest.raises(ValueError):
        WorkspaceInspectResult(
            binding=valid.binding,
            mode=valid.mode,
            observations=valid.observations,
            observed_revision="0" * 64,
            scene_may_have_changed=False,
        )
    with pytest.raises(ValueError):
        WorkspaceInspectResult(
            binding=valid.binding,
            mode=valid.mode,
            observations=valid.observations,
            observed_revision=valid.observed_revision,
            scene_may_have_changed=True,
        )


def test_result_rejects_noncanonical_wire_order() -> None:
    result = _result()
    data = result.to_dict()
    data["observations"] = list(reversed(data["observations"]))
    with pytest.raises(ValueError):
        WorkspaceInspectResult.from_dict(data)


def test_result_rejects_aggregate_payload_above_bounded_result_limit() -> None:
    observations = tuple(
        _observation(
            node_id=f"node_{index}",
            path=f"/obj/{index:04d}_" + ("x" * 900),
        )
        for index in range(400)
    )
    with pytest.raises(ValueError, match="maximum result size"):
        _result(observations=observations)


def _error() -> BridgeError:
    return BridgeError(
        code="workspace.identity_conflict",
        category="validation",
        message_for_user="The live workspace identity is ambiguous.",
        retryable=False,
        technical_detail_ref=None,
    )


def test_response_success_and_error_round_trip() -> None:
    success = WorkspaceInspectResponse(
        request_id="req_workspace_1", result=_result(), error=None
    )
    revived = parse_workspace_inspect_response(success.to_json())
    assert revived == success
    assert revived.result is not None

    error = WorkspaceInspectResponse(
        request_id="req_workspace_1", result=None, error=_error()
    )
    revived_error = parse_workspace_inspect_response(error.to_json())
    assert revived_error == error
    assert revived_error.error is not None


def test_response_requires_exactly_one_result_or_error_and_strict_json() -> None:
    with pytest.raises(ValueError):
        WorkspaceInspectResponse("req", _result(), _error())
    with pytest.raises(ValueError):
        WorkspaceInspectResponse("req", None, None)
    data = WorkspaceInspectResponse("req", _result(), None).to_dict()
    data["extra"] = True
    with pytest.raises(ValueError):
        WorkspaceInspectResponse.from_dict(data)
    duplicate = json.dumps(data).replace('"request_id": "req"', '"request_id": "req", "request_id": "x"')
    with pytest.raises(ValueError):
        parse_workspace_inspect_response(duplicate)
