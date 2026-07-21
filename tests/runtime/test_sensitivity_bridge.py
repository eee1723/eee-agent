"""Task 18-F: ``sensitivity.sample`` Bridge contracts and client tests.

DTO strictness (frozen, exact fields, duplicate keys, bounded counts/sizes,
canonical JSON) plus :meth:`BridgeClient.sample_sensitivity` driven entirely
through an injectable fake transport — no real socket, no ``hou``, no
``rpyc``. Async scenarios run via ``asyncio.run`` (this suite deliberately
avoids pytest-asyncio, matching the rest of the Runtime tests).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from typing import Awaitable, Callable

import pytest

from eee_agent.houdini_bridge.auth import BridgeIdentity, create_bridge_identity
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)
from eee_agent.houdini_bridge.sensitivity import (
    SENSITIVITY_V1,
    SensitivitySampleRequest,
    SensitivitySampleResponse,
    SensitivitySampleResult,
    SensitivitySampleTarget,
    parse_sample_request,
    parse_sample_response,
)
from eee_agent.runtime.models import canonical_json_dumps

_PROTO = PROTOCOL


# --------------------------------------------------------------------------
# test helpers
# --------------------------------------------------------------------------


def async_test(coro: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    @functools.wraps(coro)
    def wrapper() -> None:
        asyncio.run(coro())

    return wrapper


def _frame(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ack_frame(ok: bool = True, caps: list[str] | None = None) -> bytes:
    ack: dict[str, object] = {"protocol": _PROTO, "kind": "hello", "ok": ok}
    if caps is not None:
        ack["capabilities"] = caps
    return _frame(_dumps(ack))


def _parse_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        start = i + 4
        frames.append(bytes(data[start : start + length]))
        i = start + length
    return frames


def _binding(**overrides: object) -> SceneBinding:
    values: dict[str, object] = {
        "instance_id": "hou_instance_1",
        "scene_epoch": 1,
        "hip_path": None,
        "observed_revision": "sha256:abc",
    }
    values.update(overrides)
    return SceneBinding(**values)  # type: ignore[arg-type]


def _query(points: int = 8) -> SceneQueryResult:
    return SceneQueryResult(
        binding=_binding(),
        selected_nodes=(),
        nodes=(
            SelectedNode(
                path="/obj/ws/box1",
                node_type="box",
                parent_path="/obj/ws",
                display_name="box1",
                is_locked=False,
                geometry_stats={"points": points, "primitives": 6},
            ),
        ),
    )


def _result() -> SensitivitySampleResult:
    return SensitivitySampleResult(
        baseline=_query(8),
        samples=(_query(9),),
        restored=_query(8),
    )


def _target(**overrides: object) -> SensitivitySampleTarget:
    values: dict[str, object] = {
        "node_id": "n_box",
        "path": "/obj/ws/box1",
        "parm_name": "sizex",
        "value": 3.0,
    }
    values.update(overrides)
    return SensitivitySampleTarget(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> SensitivitySampleRequest:
    values: dict[str, object] = {
        "request_id": "req_sample",
        "deadline_ms": 5000,
        "scene_epoch": 1,
        "node_paths": ["/obj/ws/box1"],
        "samples": [_target()],
    }
    values.update(overrides)
    return SensitivitySampleRequest.build(**values)  # type: ignore[arg-type]


def _success_frame(request_id: str) -> bytes:
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": _result().to_dict(),
    }
    return _frame(_dumps(response))


def _error_frame(request_id: str, error: BridgeError) -> bytes:
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": False,
        "error": error.to_dict(),
    }
    return _frame(_dumps(response))


class FakeTransport:
    """In-memory framed transport (mirrors the bridge client test fake)."""

    def __init__(self, inbox: bytes = b"", *, block_when_empty: bool = False) -> None:
        self._inbox = bytearray(inbox)
        self.outbox = bytearray()
        self.block_when_empty = block_when_empty
        self.close_count = 0

    async def read_exactly(self, n: int) -> bytes:
        while len(self._inbox) < n:
            if self.block_when_empty:
                await asyncio.Event().wait()  # block until cancelled
            partial = bytes(self._inbox)
            self._inbox.clear()
            raise asyncio.IncompleteReadError(partial, n)
        chunk = bytes(self._inbox[:n])
        del self._inbox[:n]
        return chunk

    async def write(self, data: bytes) -> None:
        self.outbox.extend(data)

    async def close(self) -> None:
        self.close_count += 1


def _client(
    fake: FakeTransport, identity: BridgeIdentity | None = None
) -> BridgeClient:
    return BridgeClient(
        host="127.0.0.1",
        port=18811,
        identity=identity or create_bridge_identity(),
        transport_factory=lambda: fake,
    )


# --------------------------------------------------------------------------
# request/result/response DTO strictness
# --------------------------------------------------------------------------


def test_request_round_trip_is_canonical_json() -> None:
    request = _request()
    assert request.to_json() == canonical_json_dumps(request.to_dict())
    parsed = parse_sample_request(request.to_json())
    assert parsed == request


def test_request_envelope_shape_matches_bridge_convention() -> None:
    obj = json.loads(_request().to_json())
    assert set(obj) == {
        "protocol", "kind", "request_id", "operation",
        "deadline_ms", "scene_epoch", "payload",
    }
    assert obj["protocol"] == "eee.bridge/1"
    assert obj["kind"] == "request"
    assert obj["operation"] == "sensitivity.sample"
    assert set(obj["payload"]) == {"node_paths", "samples"}
    assert set(obj["payload"]["samples"][0]) == {"node_id", "path", "parm_name", "value"}


def test_dtos_are_frozen() -> None:
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.request_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _target().value = 9.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _result().restored = _query()  # type: ignore[misc]
    response = SensitivitySampleResponse(
        request_id="req_sample", result=_result(), error=None
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.result = None  # type: ignore[misc]


def test_request_rejects_unknown_envelope_field() -> None:
    obj = json.loads(_request().to_json())
    obj["extra"] = True
    with pytest.raises(ValueError):
        parse_sample_request(_dumps(obj))


def test_request_rejects_wrong_operation_tag() -> None:
    obj = json.loads(_request().to_json())
    obj["operation"] = "changeset.apply"
    with pytest.raises(ValueError):
        parse_sample_request(_dumps(obj))


def test_request_rejects_duplicate_json_keys() -> None:
    raw = (
        '{"protocol":"eee.bridge/1","protocol":"eee.bridge/1","kind":"request",'
        '"request_id":"req_sample","operation":"sensitivity.sample",'
        '"deadline_ms":5000,"scene_epoch":1,'
        '"payload":{"node_paths":["/obj/ws/box1"],'
        '"samples":[{"node_id":"n_box","path":"/obj/ws/box1",'
        '"parm_name":"sizex","value":3.0}]}}'
    )
    with pytest.raises(ValueError):
        parse_sample_request(raw)


def test_request_rejects_oversized_input() -> None:
    with pytest.raises(ValueError):
        parse_sample_request(" " * (MAX_MESSAGE_BYTES + 1))


def test_sample_count_is_bounded() -> None:
    with pytest.raises(ValueError):
        _request(samples=[])
    with pytest.raises(ValueError):
        _request(samples=[_target() for _ in range(17)])
    assert len(_request(samples=[_target() for _ in range(16)]).samples) == 16


def test_node_paths_are_bounded_and_absolute() -> None:
    with pytest.raises(ValueError):
        _request(node_paths=[])
    with pytest.raises(ValueError):
        _request(node_paths=["obj/ws/box1"])
    with pytest.raises(ValueError):
        _request(node_paths=["/obj/ws/box1", "/obj/ws/box1"])
    with pytest.raises(ValueError):
        _request(node_paths=[f"/obj/n{i}" for i in range(65)])


def test_sample_value_must_be_finite_numeric() -> None:
    with pytest.raises(TypeError):
        _target(value=True)
    with pytest.raises(TypeError):
        _target(value="3.0")
    with pytest.raises(ValueError):
        _target(value=float("nan"))
    with pytest.raises(ValueError):
        _target(value=float("inf"))
    assert _target(value=3).value == 3


def test_target_fields_are_strict() -> None:
    with pytest.raises(ValueError):
        _target(node_id="bad id!")
    with pytest.raises(ValueError):
        _target(parm_name="bad-parm")
    with pytest.raises(ValueError):
        _target(path="relative/path")
    assert _target(node_id=None).node_id is None


def test_result_requires_bounded_typed_samples() -> None:
    with pytest.raises(ValueError):
        SensitivitySampleResult(baseline=_query(), samples=(), restored=_query())
    with pytest.raises(ValueError):
        SensitivitySampleResult(
            baseline=_query(), samples=tuple(_query() for _ in range(17)), restored=_query()
        )
    with pytest.raises(TypeError):
        SensitivitySampleResult(
            baseline="not-a-query", samples=(_query(),), restored=_query()  # type: ignore[arg-type]
        )


def test_result_size_is_bounded() -> None:
    big_nodes = tuple(
        SelectedNode(
            path=f"/obj/ws/n{i}",
            node_type="box",
            parent_path="/obj/ws",
            display_name=f"n{i}",
            is_locked=False,
            geometry_stats={"points": i, "primitives": i},
        )
        for i in range(2400)
    )
    big_query = SceneQueryResult(
        binding=_binding(), selected_nodes=(), nodes=big_nodes
    )
    with pytest.raises(ValueError):
        SensitivitySampleResult(
            baseline=big_query, samples=(_query(),), restored=_query()
        )


def test_response_carries_exactly_one_of_result_or_error() -> None:
    with pytest.raises(ValueError):
        SensitivitySampleResponse(request_id="req_sample", result=None, error=None)
    error = BridgeError(
        code="sensitivity.cook_failed",
        category="cook",
        message_for_user="bounded",
        retryable=True,
        technical_detail_ref=None,
    )
    with pytest.raises(ValueError):
        SensitivitySampleResponse(
            request_id="req_sample", result=_result(), error=error
        )
    parsed = parse_sample_response(_error_frame("req_sample", error)[4:].decode("utf-8"))
    assert parsed.error is not None and parsed.error.code == "sensitivity.cook_failed"


def test_response_rejects_result_and_error_together_on_wire() -> None:
    obj = json.loads(_success_frame("req_sample")[4:].decode("utf-8"))
    obj["error"] = BridgeError(
        code="x.y", category="internal", message_for_user="bounded", retryable=False,
        technical_detail_ref=None,
    ).to_dict()
    with pytest.raises(ValueError):
        parse_sample_response(_dumps(obj))


# --------------------------------------------------------------------------
# client layer (FakeTransport)
# --------------------------------------------------------------------------


@async_test
async def test_sample_without_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["changeset.v1", "workspace.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.sample_sensitivity(_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert exc.value.retryable is False
    # Only the hello frame was sent; the gated request never hit the wire.
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_sample_round_trip_returns_typed_evidence() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=["changeset.v1", SENSITIVITY_V1, "workspace.v1"])
        + _success_frame("req_sample")
    )
    client = _client(fake)
    await client.open()
    request = _request()
    result = await client.sample_sensitivity(request)
    assert isinstance(result, SensitivitySampleResult)
    assert result.baseline.binding.instance_id == "hou_instance_1"
    assert len(result.samples) == 1
    assert result.restored.nodes[0].geometry_stats == {"points": 8, "primitives": 6}
    frames = _parse_frames(bytes(fake.outbox))
    assert frames[1] == request.to_json().encode("utf-8")


@async_test
async def test_sample_server_error_is_structured_and_connection_stays_open() -> None:
    error = BridgeError(
        code="sensitivity.restore_failed",
        category="restore",
        message_for_user="bounded restore failure",
        retryable=False,
        technical_detail_ref=None,
    )
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SENSITIVITY_V1]) + _error_frame("req_sample", error)
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.sample_sensitivity(_request())
    assert exc.value.code == "sensitivity.restore_failed"
    assert exc.value.category == "restore"
    assert exc.value.retryable is False
    assert fake.close_count == 0


@async_test
async def test_sample_malformed_response_aborts_connection() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SENSITIVITY_V1]) + _frame("not json")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.sample_sensitivity(_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_sample_response_request_id_mismatch_aborts() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[SENSITIVITY_V1]) + _success_frame("other_id")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.sample_sensitivity(_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_sample_requires_typed_request_and_open_client() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[SENSITIVITY_V1]))
    client = _client(fake)
    await client.open()
    with pytest.raises(TypeError):
        await client.sample_sensitivity("not-a-request")  # type: ignore[arg-type]
    # Unopened: no capabilities are known, so the gate fails closed first
    # (same order as the changeset.apply client method).
    unopened = _client(FakeTransport(inbox=_ack_frame(ok=True, caps=[SENSITIVITY_V1])))
    with pytest.raises(BridgeClientError) as exc:
        await unopened.sample_sensitivity(_request())
    assert exc.value.code == "bridge.capability_unavailable"
