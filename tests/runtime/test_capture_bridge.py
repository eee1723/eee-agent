"""Task 19-A: ``capture.capture`` Bridge contracts and client tests.

DTO strictness (frozen, exact fields, duplicate keys, bounded counts/sizes,
canonical JSON) plus :meth:`BridgeClient.capture` driven entirely through an
injectable fake transport — no real socket, no ``hou``, no ``rpyc``. Async
scenarios run via ``asyncio.run`` (this suite deliberately avoids
pytest-asyncio, matching the rest of the Runtime tests).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from typing import Awaitable, Callable

import pytest

from eee_agent.houdini_bridge.auth import BridgeIdentity, create_bridge_identity
from eee_agent.houdini_bridge.capture import (
    CAPTURE_V1,
    CaptureFramingReport,
    CaptureRequest,
    CaptureResponse,
    CaptureResult,
    CaptureSettings,
    parse_capture_request,
    parse_capture_response,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
)
from eee_agent.runtime.models import canonical_json_dumps

_PROTO = PROTOCOL
ART = f"art_{'a' * 32}"


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


def _framing(**overrides: object) -> CaptureFramingReport:
    values: dict[str, object] = {
        "adjustments_used": 1,
        "margin_left": 0.16,
        "margin_right": 0.17,
        "margin_bottom": 0.12,
        "margin_top": 0.12,
        "longest_axis_ratio": 0.78,
        "center_offset": 0.002,
    }
    values.update(overrides)
    return CaptureFramingReport(**values)  # type: ignore[arg-type]


def _result(**overrides: object) -> CaptureResult:
    values: dict[str, object] = {
        "artifact_id": ART,
        "relative_path": f"{ART}.png",
        "sha256": "b" * 64,
        "media_type": "image/png",
        "size_bytes": 4096,
        "framing": _framing(),
    }
    values.update(overrides)
    return CaptureResult(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> CaptureRequest:
    values: dict[str, object] = {
        "request_id": "req_capture",
        "deadline_ms": 5000,
        "scene_epoch": 1,
        "node_paths": ["/obj/ws/box1"],
        "target_dir": "E:/runtime/state/artifacts/ses/run",
        "artifact_id": ART,
    }
    values.update(overrides)
    return CaptureRequest.build(**values)  # type: ignore[arg-type]


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
# request/settings/result/response DTO strictness
# --------------------------------------------------------------------------


def test_request_round_trip_is_canonical_json() -> None:
    request = _request()
    assert request.to_json() == canonical_json_dumps(request.to_dict())
    parsed = parse_capture_request(request.to_json())
    assert parsed == request


def test_request_envelope_shape_matches_bridge_convention() -> None:
    obj = json.loads(_request().to_json())
    assert set(obj) == {
        "protocol", "kind", "request_id", "operation",
        "deadline_ms", "scene_epoch", "payload",
    }
    assert obj["protocol"] == "eee.bridge/1"
    assert obj["kind"] == "request"
    assert obj["operation"] == "capture.capture"
    assert set(obj["payload"]) == {"node_paths", "target_dir", "artifact_id", "settings"}
    assert obj["payload"]["artifact_id"] == ART
    assert set(obj["payload"]["settings"]) == {
        "width", "height", "preflight_width", "preflight_height",
        "margin_min", "longest_axis_min", "longest_axis_max",
        "center_offset_max", "max_adjustments",
    }


def test_request_file_name_derives_from_artifact_id() -> None:
    assert _request().file_name == f"{ART}.png"


def test_dtos_are_frozen() -> None:
    request = _request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.request_id = "other"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        CaptureSettings().width = 320  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _framing().adjustments_used = 9  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _result().sha256 = "c" * 64  # type: ignore[misc]
    response = CaptureResponse(request_id="req_capture", result=_result(), error=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.result = None  # type: ignore[misc]


def test_request_rejects_unknown_envelope_field() -> None:
    obj = json.loads(_request().to_json())
    obj["extra"] = True
    with pytest.raises(ValueError):
        parse_capture_request(_dumps(obj))


def test_request_rejects_wrong_operation_tag() -> None:
    obj = json.loads(_request().to_json())
    obj["operation"] = "changeset.apply"
    with pytest.raises(ValueError):
        parse_capture_request(_dumps(obj))


def test_request_rejects_duplicate_json_keys() -> None:
    raw = (
        '{"protocol":"eee.bridge/1","protocol":"eee.bridge/1","kind":"request",'
        '"request_id":"req_capture","operation":"capture.capture",'
        '"deadline_ms":5000,"scene_epoch":1,'
        '"payload":{"node_paths":["/obj/ws/box1"],'
        '"target_dir":"E:/a","artifact_id":"' + ART + '",'
        '"settings":{"width":1280,"height":960,"preflight_width":640,'
        '"preflight_height":480,"margin_min":0.06,"longest_axis_min":0.72,'
        '"longest_axis_max":0.84,"center_offset_max":0.03,"max_adjustments":2}}}'
    )
    with pytest.raises(ValueError):
        parse_capture_request(raw)


def test_request_rejects_oversized_input() -> None:
    with pytest.raises(ValueError):
        parse_capture_request(" " * (MAX_MESSAGE_BYTES + 1))


def test_node_paths_are_bounded_and_absolute() -> None:
    with pytest.raises(ValueError):
        _request(node_paths=[])
    with pytest.raises(ValueError):
        _request(node_paths=["obj/ws/box1"])
    with pytest.raises(ValueError):
        _request(node_paths=["/obj/ws/box1", "/obj/ws/box1"])
    with pytest.raises(ValueError):
        _request(node_paths=[f"/obj/n{i}" for i in range(65)])


def test_target_dir_must_be_absolute_and_bounded() -> None:
    with pytest.raises(ValueError):
        _request(target_dir="relative/dir")
    with pytest.raises(ValueError):
        _request(target_dir="")
    with pytest.raises(ValueError):
        _request(target_dir="E:/" + "a" * 2000)
    with pytest.raises(ValueError):
        _request(target_dir="E:/bad\x01dir")
    assert _request(target_dir="E:/artifacts/ses/run").target_dir.startswith("E:/")
    assert _request(target_dir="E:\\artifacts\\ses").target_dir.endswith("ses")
    assert _request(target_dir="/posix/absolute").target_dir == "/posix/absolute"


def test_artifact_id_must_be_a_typed_artifact_id() -> None:
    with pytest.raises(ValueError):
        _request(artifact_id="run_" + "0" * 32)
    with pytest.raises(ValueError):
        _request(artifact_id="art_short")
    with pytest.raises(TypeError):
        _request(artifact_id=123)


def test_settings_defaults_are_the_canonical_constants() -> None:
    settings = CaptureSettings()
    assert (settings.width, settings.height) == (1280, 960)
    assert (settings.preflight_width, settings.preflight_height) == (640, 480)
    assert settings.margin_min == 0.06
    assert (settings.longest_axis_min, settings.longest_axis_max) == (0.72, 0.84)
    assert settings.center_offset_max == 0.03
    assert settings.max_adjustments == 2


def test_settings_ranges_are_strict() -> None:
    with pytest.raises(ValueError):
        CaptureSettings(width=8)
    with pytest.raises(ValueError):
        CaptureSettings(width=1279)  # odd
    with pytest.raises(ValueError):
        CaptureSettings(preflight_width=320, preflight_height=200)  # aspect drift
    with pytest.raises(ValueError):
        CaptureSettings(margin_min=0.0)
    with pytest.raises(ValueError):
        CaptureSettings(longest_axis_min=0.9)
    with pytest.raises(ValueError):
        CaptureSettings(center_offset_max=0.5)
    with pytest.raises(ValueError):
        CaptureSettings(max_adjustments=3)
    with pytest.raises(TypeError):
        CaptureSettings(width=1280.0)  # type: ignore[arg-type]


def test_framing_report_bounds_are_strict() -> None:
    with pytest.raises(ValueError):
        _framing(adjustments_used=3)
    with pytest.raises(TypeError):
        _framing(adjustments_used=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        _framing(margin_left=float("nan"))
    with pytest.raises(TypeError):
        _framing(center_offset="0.01")  # type: ignore[arg-type]


def test_result_fields_are_strict() -> None:
    with pytest.raises(ValueError):
        _result(sha256="B" * 64)  # uppercase hex
    with pytest.raises(ValueError):
        _result(media_type="image/jpeg")
    with pytest.raises(ValueError):
        _result(size_bytes=0)
    with pytest.raises(ValueError):
        _result(relative_path="../escape.png")
    with pytest.raises(ValueError):
        _result(relative_path="nested/dir.png")
    with pytest.raises(ValueError):
        _result(artifact_id="chg_" + "0" * 32)


def test_response_carries_exactly_one_of_result_or_error() -> None:
    with pytest.raises(ValueError):
        CaptureResponse(request_id="req_capture", result=None, error=None)
    error = BridgeError(
        code="capture.framing_failed",
        category="framing",
        message_for_user="bounded",
        retryable=False,
        technical_detail_ref=None,
    )
    with pytest.raises(ValueError):
        CaptureResponse(request_id="req_capture", result=_result(), error=error)
    parsed = parse_capture_response(_error_frame("req_capture", error)[4:].decode("utf-8"))
    assert parsed.error is not None and parsed.error.code == "capture.framing_failed"


def test_response_rejects_result_and_error_together_on_wire() -> None:
    obj = json.loads(_success_frame("req_capture")[4:].decode("utf-8"))
    obj["error"] = BridgeError(
        code="x.y", category="internal", message_for_user="bounded", retryable=False,
        technical_detail_ref=None,
    ).to_dict()
    with pytest.raises(ValueError):
        parse_capture_response(_dumps(obj))


# --------------------------------------------------------------------------
# client layer (FakeTransport)
# --------------------------------------------------------------------------


@async_test
async def test_capture_without_capability_sends_no_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=["changeset.v1", "sensitivity.v1"]))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.capture(_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert exc.value.retryable is False
    # Only the hello frame was sent; the gated request never hit the wire.
    assert len(_parse_frames(bytes(fake.outbox))) == 1


@async_test
async def test_capture_round_trip_returns_typed_reference() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[CAPTURE_V1, "changeset.v1", "sensitivity.v1"])
        + _success_frame("req_capture")
    )
    client = _client(fake)
    await client.open()
    request = _request()
    result = await client.capture(request)
    assert isinstance(result, CaptureResult)
    assert result.artifact_id == ART
    assert result.sha256 == "b" * 64
    assert result.media_type == "image/png"
    assert result.size_bytes == 4096
    assert result.framing.longest_axis_ratio == 0.78
    frames = _parse_frames(bytes(fake.outbox))
    assert frames[1] == request.to_json().encode("utf-8")


@async_test
async def test_capture_server_error_is_structured_and_connection_stays_open() -> None:
    error = BridgeError(
        code="capture.cleanup_failed",
        category="cleanup",
        message_for_user="bounded cleanup failure",
        retryable=False,
        technical_detail_ref=None,
    )
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[CAPTURE_V1]) + _error_frame("req_capture", error)
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.capture(_request())
    assert exc.value.code == "capture.cleanup_failed"
    assert exc.value.category == "cleanup"
    assert exc.value.retryable is False
    assert fake.close_count == 0


@async_test
async def test_capture_malformed_response_aborts_connection() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[CAPTURE_V1]) + _frame("not json")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.capture(_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_capture_response_request_id_mismatch_aborts() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(ok=True, caps=[CAPTURE_V1]) + _success_frame("other_id")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.capture(_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


@async_test
async def test_capture_requires_typed_request_and_open_client() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True, caps=[CAPTURE_V1]))
    client = _client(fake)
    await client.open()
    with pytest.raises(TypeError):
        await client.capture("not-a-request")  # type: ignore[arg-type]
    # Unopened: no capabilities are known, so the gate fails closed first
    # (same order as the changeset.apply client method).
    unopened = _client(FakeTransport(inbox=_ack_frame(ok=True, caps=[CAPTURE_V1])))
    with pytest.raises(BridgeClientError) as exc:
        await unopened.capture(_request())
    assert exc.value.code == "bridge.capability_unavailable"
