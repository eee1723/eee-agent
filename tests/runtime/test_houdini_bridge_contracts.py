"""Task 15-A: strict read-only HoudiniBridge DTO and error contract tests.

Pure-Python RED/GREEN tests for the frozen request/response/error DTOs and the
canonical JSON contract in :mod:`eee_agent.houdini_bridge.contracts`. No ``hou``
or ``rpyc`` import is exercised here; these tests are fully offline and never
touch the legacy bridge, a socket, or the Houdini process.
"""

from __future__ import annotations

import inspect
import json

import pytest

from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    BridgeOperation,
    BridgeRequest,
    BridgeResponse,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
    parse_request,
)

_REQUEST_ID = "req_0123456789abcdef0123456789abcdef"

# Canonical compact sorted UTF-8 JSON for the default request envelope.
_EXPECTED_REQUEST_JSON = (
    '{"deadline_ms":5000,"kind":"request","operation":"scene.query",'
    '"payload":{"include_geometry_stats":true,"include_selection":true,'
    '"node_paths":[]},"protocol":"eee.bridge/1",'
    '"request_id":"' + _REQUEST_ID + '","scene_epoch":1}'
)


# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "include_selection": True,
        "node_paths": [],
        "include_geometry_stats": True,
    }
    base.update(overrides)
    return base


def _request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "protocol": "eee.bridge/1",
        "kind": "request",
        "request_id": _REQUEST_ID,
        "operation": "scene.query",
        "deadline_ms": 5000,
        "scene_epoch": 1,
        "payload": _payload(),
    }
    base.update(overrides)
    return base


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = dict(
        instance_id="hou_instance_1",
        scene_epoch=1,
        hip_path="C:/project/scene.hip",
        observed_revision="sha256:deadbeef",
    )
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _node(**overrides: object) -> SelectedNode:
    values: dict[str, object] = dict(
        path="/obj/geo1",
        node_type="geo",
        parent_path="/obj",
        display_name="geo1",
        is_locked=False,
        geometry_stats={"points": 8, "primitives": 6},
    )
    values.update(overrides)
    return SelectedNode(**values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# constants + enum
# --------------------------------------------------------------------------


def test_protocol_constant_is_exact() -> None:
    assert PROTOCOL == "eee.bridge/1"


def test_max_message_bytes_is_one_mebibyte() -> None:
    assert MAX_MESSAGE_BYTES == 1_048_576


def test_bridge_operation_scene_query_value() -> None:
    assert BridgeOperation.SCENE_QUERY == "scene.query"
    assert BridgeOperation.SCENE_QUERY.value == "scene.query"
    assert isinstance(BridgeOperation.SCENE_QUERY, str)


# --------------------------------------------------------------------------
# 1. valid request + 2. canonical JSON
# --------------------------------------------------------------------------


def test_valid_scene_query_request() -> None:
    request = BridgeRequest.from_dict(_request())
    assert type(request) is BridgeRequest
    assert request.request_id == _REQUEST_ID
    assert request.operation is BridgeOperation.SCENE_QUERY
    assert request.deadline_ms == 5000
    assert request.scene_epoch == 1
    assert set(request.payload.keys()) == {
        "include_selection",
        "node_paths",
        "include_geometry_stats",
    }
    assert request.payload["include_selection"] is True
    assert request.payload["include_geometry_stats"] is True
    # node_paths is deep-frozen to a tuple
    assert request.payload["node_paths"] == ()


def test_canonical_compact_sorted_json_output() -> None:
    request = BridgeRequest.from_dict(_request())
    assert request.to_json() == _EXPECTED_REQUEST_JSON
    data = request.to_dict()
    assert data["protocol"] == "eee.bridge/1"
    assert data["kind"] == "request"
    assert data["operation"] == "scene.query"
    assert data["payload"]["node_paths"] == []


# --------------------------------------------------------------------------
# 3-6. envelope rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "protocol",
        "kind",
        "request_id",
        "operation",
        "deadline_ms",
        "scene_epoch",
        "payload",
    ],
)
def test_missing_envelope_field_rejected(field: str) -> None:
    bad = _request()
    del bad[field]
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(bad)


def test_extra_envelope_field_rejected() -> None:
    bad = _request()
    bad["unexpected"] = 1
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(bad)


def test_from_dict_rejects_non_dict_envelope() -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict([1, 2, 3])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "protocol", ["eee.bridge/2", "eee.runtime/1", "something/else"]
)
def test_wrong_protocol_rejected(protocol: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(protocol=protocol))


@pytest.mark.parametrize(
    "operation", ["scene.mutate", "scenequery", "", "scene.query.extra"]
)
def test_wrong_operation_rejected(operation: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(operation=operation))


# --------------------------------------------------------------------------
# 7. request_id malformed / empty / overlong
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_id",
    [123, "", "r" * 129],
    ids=["non-string", "empty", "overlong"],
)
def test_request_id_malformed_empty_overlong_rejected(request_id: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(request_id=request_id))


def test_request_id_128_chars_accepted() -> None:
    request = BridgeRequest.from_dict(_request(request_id="r" * 128))
    assert len(request.request_id) == 128


# --------------------------------------------------------------------------
# 8-10. deadline_ms exact int, range 1..30000
# --------------------------------------------------------------------------


def test_bool_deadline_ms_rejected() -> None:
    with pytest.raises(TypeError):
        BridgeRequest.from_dict(_request(deadline_ms=True))


@pytest.mark.parametrize("deadline_ms", [0, -1, -100])
def test_deadline_ms_zero_or_negative_rejected(deadline_ms: int) -> None:
    with pytest.raises(ValueError):
        BridgeRequest.from_dict(_request(deadline_ms=deadline_ms))


@pytest.mark.parametrize("deadline_ms", [30001, 100000])
def test_deadline_ms_over_max_rejected(deadline_ms: int) -> None:
    with pytest.raises(ValueError):
        BridgeRequest.from_dict(_request(deadline_ms=deadline_ms))


@pytest.mark.parametrize("deadline_ms", [1, 30000])
def test_deadline_ms_boundaries_accepted(deadline_ms: int) -> None:
    request = BridgeRequest.from_dict(_request(deadline_ms=deadline_ms))
    assert request.deadline_ms == deadline_ms


# --------------------------------------------------------------------------
# 11-12. scene_epoch exact int, >= 1
# --------------------------------------------------------------------------


def test_bool_scene_epoch_rejected() -> None:
    with pytest.raises(TypeError):
        BridgeRequest.from_dict(_request(scene_epoch=True))


def test_float_scene_epoch_rejected() -> None:
    with pytest.raises(TypeError):
        BridgeRequest.from_dict(_request(scene_epoch=1.0))


@pytest.mark.parametrize("scene_epoch", [0, -1])
def test_scene_epoch_below_one_rejected(scene_epoch: int) -> None:
    with pytest.raises(ValueError):
        BridgeRequest.from_dict(_request(scene_epoch=scene_epoch))


# --------------------------------------------------------------------------
# 13-14. payload exact dict + known fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [[], "not-a-dict", 42, None],
    ids=["list", "str", "int", "none"],
)
def test_payload_wrong_type_rejected(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(payload=payload))


def test_payload_unknown_field_rejected() -> None:
    bad_payload = _payload()
    bad_payload["unexpected"] = 1
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(payload=bad_payload))


def test_payload_partial_keys_accepted() -> None:
    # node_paths (and the booleans) are optional per spec section 3.1.
    request = BridgeRequest.from_dict(
        _request(payload={"node_paths": ["/obj"]})
    )
    assert request.payload["node_paths"] == ("/obj",)


# --------------------------------------------------------------------------
# 15-17. node_paths typing, absolute paths, capacity
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "node_paths",
    ["obj/geo1", 42, [1, 2], ["/obj", 1]],
    ids=["str", "int", "non-str-items", "mixed"],
)
def test_node_paths_wrong_type_rejected(node_paths: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(payload=_payload(node_paths=node_paths)))


@pytest.mark.parametrize(
    "path", ["obj/geo1", "geo1", "obj", "./obj/geo1"]
)
def test_relative_node_path_rejected(path: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeRequest.from_dict(_request(payload=_payload(node_paths=[path])))


def test_absolute_node_paths_accepted() -> None:
    request = BridgeRequest.from_dict(
        _request(payload=_payload(node_paths=["/obj", "/obj/geo1", "/"]))
    )
    assert request.payload["node_paths"] == ("/obj", "/obj/geo1", "/")


def test_node_paths_over_128_rejected() -> None:
    paths = [f"/obj/n{i}" for i in range(129)]
    with pytest.raises(ValueError):
        BridgeRequest.from_dict(_request(payload=_payload(node_paths=paths)))


def test_node_paths_128_accepted() -> None:
    paths = [f"/obj/n{i}" for i in range(128)]
    request = BridgeRequest.from_dict(_request(payload=_payload(node_paths=paths)))
    assert len(request.payload["node_paths"]) == 128


# --------------------------------------------------------------------------
# 18-19. duplicate JSON keys (text parse path)
# --------------------------------------------------------------------------


def test_duplicate_top_level_json_key_rejected() -> None:
    raw = (
        '{"protocol":"eee.bridge/1","kind":"request",'
        '"request_id":"' + _REQUEST_ID + '",'
        '"request_id":"' + _REQUEST_ID + '",'
        '"operation":"scene.query","deadline_ms":5000,"scene_epoch":1,'
        '"payload":{"include_selection":true,"node_paths":[],'
        '"include_geometry_stats":true}}'
    )
    with pytest.raises(ValueError):
        parse_request(raw)


def test_duplicate_nested_payload_key_rejected() -> None:
    raw = (
        '{"protocol":"eee.bridge/1","kind":"request",'
        '"request_id":"' + _REQUEST_ID + '",'
        '"operation":"scene.query","deadline_ms":5000,"scene_epoch":1,'
        '"payload":{"include_selection":true,"include_selection":false,'
        '"node_paths":[],"include_geometry_stats":true}}'
    )
    with pytest.raises(ValueError):
        parse_request(raw)


# --------------------------------------------------------------------------
# 20-22. NaN/Infinity, non-string keys, cycle (geometry_stats freeze surface)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "inf", "-inf"]
)
def test_non_finite_geometry_stats_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        SelectedNode(
            path="/obj/geo1",
            node_type="geo",
            parent_path="/obj",
            display_name="geo1",
            is_locked=False,
            geometry_stats={"points": value},
        )


def test_non_string_geometry_stats_key_rejected() -> None:
    with pytest.raises(TypeError):
        SelectedNode(
            path="/obj/geo1",
            node_type="geo",
            parent_path="/obj",
            display_name="geo1",
            is_locked=False,
            geometry_stats={1: "x"},
        )


def test_cycle_in_geometry_stats_rejected() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError):
        SelectedNode(
            path="/obj/geo1",
            node_type="geo",
            parent_path="/obj",
            display_name="geo1",
            is_locked=False,
            geometry_stats=cyclic,
        )


# --------------------------------------------------------------------------
# 23. oversize UTF-8 JSON > 1 MiB (text parse path)
# --------------------------------------------------------------------------


def test_oversize_json_rejected() -> None:
    huge_path = "/" + "x" * (MAX_MESSAGE_BYTES + 1)
    raw = json.dumps(_request(payload=_payload(node_paths=[huge_path])))
    assert len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES
    with pytest.raises(ValueError):
        parse_request(raw)


# --------------------------------------------------------------------------
# 24-25. snapshot independence + frozen tuple/mapping behavior
# --------------------------------------------------------------------------


def test_mutable_input_snapshot_independence() -> None:
    source_payload = _payload(node_paths=["/obj/a"])
    request = BridgeRequest.from_dict(_request(payload=source_payload))
    source_payload["node_paths"].append("/obj/b")
    source_payload["include_selection"] = False
    source_payload["extra"] = 1
    assert request.payload["node_paths"] == ("/obj/a",)
    assert request.payload["include_selection"] is True
    assert "extra" not in request.payload


def test_payload_is_frozen_mapping_and_tuple() -> None:
    request = BridgeRequest.from_dict(
        _request(payload=_payload(node_paths=["/obj/a"]))
    )
    # MappingProxyType is immutable.
    with pytest.raises(TypeError):
        request.payload["include_selection"] = False  # type: ignore[index]
    # node_paths is deep-frozen to a tuple.
    assert type(request.payload["node_paths"]) is tuple
    # The dataclass itself is frozen.
    with pytest.raises(AttributeError):
        request.deadline_ms = 1  # type: ignore[misc]


# --------------------------------------------------------------------------
# 26. BridgeError structured fields
# --------------------------------------------------------------------------


def test_bridge_error_structured_fields() -> None:
    error = BridgeError(
        code="bridge.stale_scene",
        category="stale_scene",
        message_for_user="The Houdini scene changed; refresh before continuing.",
        retryable=True,
        technical_detail_ref="err_abc",
    )
    assert error.to_dict() == {
        "code": "bridge.stale_scene",
        "category": "stale_scene",
        "message_for_user": "The Houdini scene changed; refresh before continuing.",
        "retryable": True,
        "technical_detail_ref": "err_abc",
    }


def test_bridge_error_accepts_null_technical_detail_ref() -> None:
    error = BridgeError(
        code="bridge.invalid_request",
        category="validation",
        message_for_user="bad",
        retryable=False,
        technical_detail_ref=None,
    )
    assert error.technical_detail_ref is None
    assert error.to_dict()["technical_detail_ref"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            code="bridge.x",
            category="c",
            message_for_user="m",
            retryable="yes",  # not a bool
            technical_detail_ref=None,
        ),
        dict(
            code="",  # empty code
            category="c",
            message_for_user="m",
            retryable=True,
            technical_detail_ref=None,
        ),
        dict(
            code="nope",  # non-namespaced code
            category="c",
            message_for_user="m",
            retryable=True,
            technical_detail_ref=None,
        ),
        dict(
            code="bridge.x",
            category="",
            message_for_user="m",
            retryable=True,
            technical_detail_ref=None,
        ),
        dict(
            code="bridge.x",
            category="c",
            message_for_user="",
            retryable=True,
            technical_detail_ref=None,
        ),
    ],
    ids=["retryable-not-bool", "empty-code", "non-namespaced-code", "empty-category", "empty-message"],
)
def test_bridge_error_rejects_invalid_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeError(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 27-30. BridgeResponse invariants
# --------------------------------------------------------------------------


def test_bridge_response_success_invariant() -> None:
    node = _node()
    result = SceneQueryResult(
        binding=_binding(), selected_nodes=[node], nodes=[node]
    )
    response = BridgeResponse(request_id=_REQUEST_ID, result=result, error=None)
    data = response.to_dict()
    assert data["protocol"] == "eee.bridge/1"
    assert data["kind"] == "response"
    assert data["request_id"] == _REQUEST_ID
    assert data["ok"] is True
    assert "result" in data and "error" not in data
    assert data["result"]["binding"]["instance_id"] == "hou_instance_1"
    assert data["result"]["selected_nodes"][0]["path"] == "/obj/geo1"
    text = response.to_json()
    assert '"ok":true' in text


def test_bridge_response_error_invariant() -> None:
    error = BridgeError(
        code="bridge.stale_scene",
        category="stale_scene",
        message_for_user="refresh",
        retryable=True,
        technical_detail_ref=None,
    )
    response = BridgeResponse(request_id=_REQUEST_ID, result=None, error=error)
    data = response.to_dict()
    assert data["ok"] is False
    assert "error" in data and "result" not in data
    assert data["error"]["code"] == "bridge.stale_scene"
    text = response.to_json()
    assert text.startswith('{"error":')
    assert '"ok":false' in text


def test_bridge_response_result_and_error_both_rejected() -> None:
    result = SceneQueryResult(
        binding=_binding(), selected_nodes=[], nodes=[]
    )
    error = BridgeError(
        code="bridge.x",
        category="c",
        message_for_user="m",
        retryable=False,
        technical_detail_ref=None,
    )
    with pytest.raises(ValueError):
        BridgeResponse(request_id=_REQUEST_ID, result=result, error=error)


def test_bridge_response_neither_result_nor_error_rejected() -> None:
    with pytest.raises(ValueError):
        BridgeResponse(request_id=_REQUEST_ID, result=None, error=None)


def test_bridge_response_rejects_bad_request_id() -> None:
    with pytest.raises((TypeError, ValueError)):
        BridgeResponse(
            request_id="",
            result=SceneQueryResult(
                binding=_binding(), selected_nodes=[], nodes=[]
            ),
            error=None,
        )


# --------------------------------------------------------------------------
# 31. selected_nodes/nodes are immutable snapshots
# --------------------------------------------------------------------------


def test_selected_nodes_and_nodes_are_immutable_tuples() -> None:
    node = _node()
    source: list[SelectedNode] = [node]
    result = SceneQueryResult(
        binding=_binding(), selected_nodes=source, nodes=source
    )
    assert type(result.selected_nodes) is tuple
    assert type(result.nodes) is tuple
    source.append(_node(path="/obj/other"))
    assert len(result.selected_nodes) == 1
    assert len(result.nodes) == 1
    with pytest.raises(AttributeError):
        result.selected_nodes = ()  # type: ignore[misc]


def test_scene_query_result_rejects_non_selected_node_items() -> None:
    with pytest.raises((TypeError, ValueError)):
        SceneQueryResult(
            binding=_binding(), selected_nodes=["/obj"], nodes=[]  # type: ignore[list-item]
        )


def test_scene_query_result_rejects_non_sequence_nodes() -> None:
    with pytest.raises((TypeError, ValueError)):
        SceneQueryResult(
            binding=_binding(), selected_nodes="/obj", nodes=[]  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# result-side DTO field validation + snapshots
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            instance_id="",
            scene_epoch=1,
            hip_path=None,
            observed_revision="r",
        ),
        dict(
            instance_id="hou_1",
            scene_epoch=True,
            hip_path=None,
            observed_revision="r",
        ),
        dict(
            instance_id="hou_1",
            scene_epoch=0,
            hip_path=None,
            observed_revision="r",
        ),
        dict(
            instance_id="hou_1",
            scene_epoch=1,
            hip_path=None,
            observed_revision="",
        ),
    ],
    ids=["empty-instance", "bool-epoch", "zero-epoch", "empty-revision"],
)
def test_scene_binding_rejects_invalid_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        SceneBinding(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(
            path="",
            node_type="geo",
            parent_path="/obj",
            display_name="g",
            is_locked=False,
            geometry_stats=None,
        ),
        dict(
            path="/obj/a",
            node_type="geo",
            parent_path="/obj",
            display_name="g",
            is_locked="no",
            geometry_stats=None,
        ),
        dict(
            path="/obj/a",
            node_type="geo",
            parent_path="/obj",
            display_name="",
            is_locked=False,
            geometry_stats=None,
        ),
        dict(
            path="/obj/a",
            node_type="geo",
            parent_path="/obj",
            display_name="g",
            is_locked=False,
            geometry_stats=[],
        ),
    ],
    ids=["empty-path", "bool-is-locked-is-str", "empty-display", "geometry-stats-not-dict"],
)
def test_selected_node_rejects_invalid_fields(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        SelectedNode(**kwargs)  # type: ignore[arg-type]


def test_selected_node_geometry_stats_none_allowed() -> None:
    node = SelectedNode(
        path="/obj/a",
        node_type="geo",
        parent_path="/obj",
        display_name="a",
        is_locked=False,
        geometry_stats=None,
    )
    assert node.geometry_stats is None
    assert node.to_dict()["geometry_stats"] is None


def test_selected_node_geometry_stats_snapshot() -> None:
    stats = {"points": 8, "nested": {"a": [1, 2]}}
    node = _node(geometry_stats=stats)
    stats["points"] = 99
    stats["nested"]["a"].append(3)
    stats["extra"] = 1
    assert node.to_dict()["geometry_stats"] == {"points": 8, "nested": {"a": [1, 2]}}
    # the frozen mapping cannot be mutated through the DTO
    with pytest.raises(TypeError):
        node.geometry_stats["points"] = 5  # type: ignore[index]


# --------------------------------------------------------------------------
# parse_request entrypoint + round trips
# --------------------------------------------------------------------------


def test_parse_request_accepts_str() -> None:
    request = parse_request(json.dumps(_request()))
    assert request.operation is BridgeOperation.SCENE_QUERY
    assert request.scene_epoch == 1


def test_parse_request_accepts_bytes() -> None:
    request = parse_request(json.dumps(_request()).encode("utf-8"))
    assert request.operation is BridgeOperation.SCENE_QUERY


def test_parse_request_rejects_non_str_bytes() -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_request(123)  # type: ignore[arg-type]


def test_parse_request_rejects_invalid_json() -> None:
    with pytest.raises((TypeError, ValueError)):
        parse_request("{not json")


def test_request_roundtrip_through_json() -> None:
    original = BridgeRequest.from_dict(_request())
    text = original.to_json()
    reparsed = parse_request(text)
    assert reparsed.to_json() == text
    assert reparsed.request_id == original.request_id


def test_bridge_response_roundtrip() -> None:
    result = SceneQueryResult(
        binding=_binding(), selected_nodes=[_node()], nodes=[]
    )
    response = BridgeResponse(request_id=_REQUEST_ID, result=result, error=None)
    reparsed = BridgeResponse.from_dict(response.to_dict())
    assert reparsed.request_id == _REQUEST_ID
    assert reparsed.result is not None
    assert reparsed.error is None
    assert reparsed.to_dict() == response.to_dict()


def test_bridge_response_roundtrip_error() -> None:
    error = BridgeError(
        code="bridge.not_available",
        category="internal_failure",
        message_for_user="bridge down",
        retryable=True,
        technical_detail_ref="err_xyz",
    )
    response = BridgeResponse(request_id=_REQUEST_ID, result=None, error=error)
    reparsed = BridgeResponse.from_dict(response.to_dict())
    assert reparsed.error is not None
    assert reparsed.error.code == "bridge.not_available"
    assert reparsed.to_json() == response.to_json()


# --------------------------------------------------------------------------
# 32. contracts.py must not import hou or rpyc
# --------------------------------------------------------------------------


def test_contracts_does_not_import_hou_or_rpyc() -> None:
    source = inspect.getsource(
        __import__("eee_agent.houdini_bridge.contracts", fromlist=["x"])
    )
    assert "import hou" not in source
    assert "import rpyc" not in source
    assert "\nfrom hou " not in source
    assert "\nfrom rpyc " not in source
