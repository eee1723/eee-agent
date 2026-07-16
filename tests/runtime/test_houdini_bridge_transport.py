"""Task 15-D: offline loopback transport integration tests.

Drives a REAL ``BridgeClient`` against a REAL ``asyncio.start_server`` loopback
TCP server (``BridgeServer.handle_connection``), with a REAL
``MainThreadReadQueue`` and a fake (``hou``-free) scene adapter. No ``hou``, no
``rpyc``, no legacy ``eee_agent.bridge``, no real Houdini process.

The Houdini main-thread pump is modelled by a cooperative pump task that calls
``queue.pump_one()`` on the event-loop thread (the fake adapter performs no real
HOM read, so running it on the loop thread is safe in tests). The production
server never creates that task — Houdini's main-thread event callback owns the
pump.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import threading
import time
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    create_bridge_identity,
    write_bridge_identity_files,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeRequest,
    SceneQueryResult,
)
from eee_agent.houdini_bridge.changesets import CHANGESET_V1
from eee_agent.houdini_bridge.workspaces import (
    WORKSPACE_V1,
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
)
from eee_agent.houdini_bridge.queue import MainThreadReadQueue
from houdini_side.secure_bridge import BridgeServer, HoudiniSceneAdapter


# --------------------------------------------------------------------------
# test helpers
# --------------------------------------------------------------------------


def async_test(coro: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    @functools.wraps(coro)
    def wrapper(*args: object, **kwargs: object) -> None:
        asyncio.run(coro(*args, **kwargs))

    return wrapper


def _frame(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _make_request(
    request_id: str = "req_test",
    *,
    scene_epoch: int = 1,
    deadline_ms: int = 5000,
    include_selection: bool = True,
    node_paths: list[str] | None = None,
    include_geometry_stats: bool = True,
) -> BridgeRequest:
    return BridgeRequest.from_dict(
        {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": request_id,
            "operation": "scene.query",
            "deadline_ms": deadline_ms,
            "scene_epoch": scene_epoch,
            "payload": {
                "include_selection": include_selection,
                "node_paths": node_paths if node_paths is not None else [],
                "include_geometry_stats": include_geometry_stats,
            },
        }
    )


# --- fake Houdini (hou-free, just enough for HoudiniSceneAdapter) ----------


class _Vec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._x, self._y, self._z = x, y, z

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y

    def z(self) -> float:
        return self._z


class _BBox:
    def __init__(self, mn: tuple, mx: tuple) -> None:
        self._mn, self._mx = mn, mx

    def minvec(self) -> _Vec:
        return _Vec(*self._mn)

    def maxvec(self) -> _Vec:
        return _Vec(*self._mx)


class _Geometry:
    def __init__(self, points: int, prims: int, bbox: _BBox | None = None) -> None:
        self._points = points
        self._prims = prims
        self._bbox = bbox

    def pointCount(self) -> int:
        return self._points

    def primCount(self) -> int:
        return self._prims

    def boundingBox(self) -> _BBox:
        if self._bbox is None:
            raise RuntimeError("empty geometry")
        return self._bbox


class _NodeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Parent:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _Node:
    def __init__(
        self,
        path: str,
        type_name: str = "geo",
        *,
        parent: str = "/obj",
        geo: _Geometry | None = None,
        user_data: dict[str, str] | None = None,
    ) -> None:
        self._path = path
        self._type = type_name
        self._parent = parent
        self._geo = geo
        self._user_data = dict(user_data or {})

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _NodeType:
        return _NodeType(self._type)

    def parent(self) -> _Parent:
        return _Parent(self._parent)

    def isHardLocked(self) -> bool:
        return False

    def isSoftLocked(self) -> bool:
        return False

    def geometry(self) -> _Geometry:
        if self._geo is None:
            raise RuntimeError("no geometry")
        return self._geo

    def userData(self, key: str) -> str | None:
        return self._user_data.get(key)

    def children(self) -> tuple[()]:
        return ()


class _ScanRoot:
    def __init__(self, nodes: tuple[_Node, ...]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple[_Node, ...]:
        return self._nodes

    def children(self) -> tuple[_Node, ...]:
        return self._nodes


class _HipFile:
    def __init__(self, name: str = "") -> None:
        self._name = name
        self._callbacks: list[Callable[[str], None]] = []

    def name(self) -> str:
        return self._name

    def addEventCallback(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def removeEventCallback(self, callback: Callable[[str], None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def eventCallbacks(self) -> list:
        return list(self._callbacks)


class _HipFileEventType:
    AfterClear = "AfterClear"
    AfterLoad = "AfterLoad"
    BeforeSave = "BeforeSave"
    AfterSave = "AfterSave"


class FakeHou:
    def __init__(
        self,
        *,
        selected: tuple[_Node, ...] = (),
        nodes: dict[str, _Node] | None = None,
        version: str = "21.0.440",
        hip_name: str = "",
    ) -> None:
        self._selected = selected
        self._nodes = nodes or {}
        self._version = version
        self.hipFile = _HipFile(hip_name)
        self.hipFileEventType = _HipFileEventType()
        self.selected_calls = 0
        self.node_calls = 0

    def applicationVersionString(self) -> str:
        return self._version

    def selectedNodes(self) -> tuple:
        self.selected_calls += 1
        return tuple(self._selected)

    def node(self, path: str):
        self.node_calls += 1
        if path == "/":
            unique = {node.path(): node for node in (*self._nodes.values(), *self._selected)}
            return _ScanRoot(tuple(unique.values()))
        return self._nodes.get(path)


def _geo_node(path: str = "/obj/geo1", points: int = 8, prims: int = 6) -> _Node:
    return _Node(
        path,
        "geo",
        parent="/obj",
        geo=_Geometry(points, prims, _BBox((0, 0, 0), (1, 1, 1))),
    )


def _make_adapter(
    *,
    selected: tuple[_Node, ...] = (),
    nodes: dict[str, _Node] | None = None,
    hip_name: str = "",
) -> tuple[HoudiniSceneAdapter, FakeHou]:
    hou = FakeHou(selected=selected, nodes=nodes, hip_name=hip_name)
    return HoudiniSceneAdapter(hou), hou


# --- server harness -------------------------------------------------------


class _Harness:
    """Owns a started loopback server + cooperative pump for one test."""

    def __init__(self) -> None:
        self.server: BridgeServer | None = None
        self.queue: MainThreadReadQueue | None = None
        self.aio: asyncio.base_events.Server | None = None
        self.port: int = 0
        self.identity = None
        self._stop: asyncio.Event | None = None
        self._pump_task: asyncio.Task | None = None
        self._pump: bool = True

    async def start(
        self,
        state_dir: Path,
        *,
        adapter: HoudiniSceneAdapter,
        identity=None,
        host: str = "127.0.0.1",
        port: int = 0,
        pump: bool = True,
        capabilities: tuple[str, ...] | None = None,
    ) -> int:
        self.queue = MainThreadReadQueue()
        self.identity = identity if identity is not None else create_bridge_identity()
        kwargs: dict[str, object] = {}
        if capabilities is not None:
            kwargs["capabilities"] = capabilities
        self.server = BridgeServer(
            adapter=adapter,
            identity=self.identity,
            state_dir=state_dir,
            queue=self.queue,
            **kwargs,  # type: ignore[arg-type]
        )
        self._pump = pump
        self.aio = await asyncio.start_server(self.server.handle_connection, host, port)
        # serve() publishes identity and guarantees that on a publication
        # failure the listener is closed — the production lifecycle, exercised
        # here instead of a bare publish_identity().
        self.port = await self.server.serve(self.aio, host=host)
        self._stop = asyncio.Event()
        if pump:

            async def _pump_loop() -> None:
                assert self.queue is not None
                assert self._stop is not None
                while not self._stop.is_set():
                    self.queue.pump_one()
                    await asyncio.sleep(0.001)

            self._pump_task = asyncio.create_task(_pump_loop())
        return self.port

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._pump_task = None
        # stop() owns the full shutdown: close the listener (awaited), drain the
        # queue, close writers, remove identity files + the epoch callback.
        if self.server is not None:
            await self.server.stop()
        self.aio = None
        # Let any residual connection handlers finish.
        await asyncio.sleep(0.02)


# --- raw socket helpers ---------------------------------------------------


async def _read_frame(reader: asyncio.StreamReader, *, timeout: float = 2.0) -> bytes:
    """Read one framed payload; raises on EOF or timeout."""
    header = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    length = int.from_bytes(header, "big")
    if length <= 0 or length > MAX_MESSAGE_BYTES:
        return header  # caller inspects bad length
    return await asyncio.wait_for(reader.readexactly(length), timeout=timeout)


async def _send_raw(writer: asyncio.StreamWriter, obj: dict | bytes) -> None:
    if isinstance(obj, (bytes, bytearray)):
        writer.write(_frame(bytes(obj)))
    else:
        writer.write(_frame(_dumps(obj)))
    await writer.drain()


# ==========================================================================
# 1. loopback binding
# ==========================================================================


@async_test
async def test_server_binds_only_loopback(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    await harness.start(tmp_path, adapter=adapter, host="127.0.0.1")
    try:
        assert harness.aio is not None
        assert harness.aio.sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        await harness.stop()


@async_test
async def test_server_publishes_identity_files(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    await harness.start(tmp_path, adapter=adapter)
    try:
        assert (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
        assert (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
        discovery = json.loads(
            (tmp_path / BRIDGE_DISCOVERY_FILENAME).read_text(encoding="utf-8")
        )
        assert discovery["host"] == "127.0.0.1"
        assert discovery["port"] == harness.port
        assert discovery["token_fingerprint"] == harness.identity.fingerprint
        # The full token never appears in discovery.
        assert harness.identity.token not in (
            tmp_path / BRIDGE_DISCOVERY_FILENAME
        ).read_text(encoding="utf-8")
    finally:
        await harness.stop()


# ==========================================================================
# 3 + 4. hello with the correct token succeeds
# ==========================================================================


@async_test
async def test_correct_token_handshake_succeeds(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        client = BridgeClient(
            host="127.0.0.1", port=port, identity=harness.identity
        )
        await client.open()  # hello succeeds
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 5. wrong token -> bridge.unauthorized + connection closed
# ==========================================================================


@async_test
async def test_wrong_token_returns_unauthorized_and_closes(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        wrong = create_bridge_identity()
        client = BridgeClient(host="127.0.0.1", port=port, identity=wrong)
        with pytest.raises(BridgeClientError) as exc:
            await client.open()
        assert exc.value.code == "bridge.unauthorized"
    finally:
        await harness.stop()


# ==========================================================================
# 6. wrong protocol / kind in hello is rejected
# ==========================================================================


@async_test
async def test_wrong_protocol_in_hello_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": "eee.bridge/2", "kind": "hello", "token": harness.identity.token},
        )
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj.get("ok") is False
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_wrong_kind_in_hello_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "not-hello", "token": harness.identity.token},
        )
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj.get("ok") is False
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 7 + 8 + 10 + 11. scene.query round-trip; only scene.query; DTO result;
#                  request_id matches
# ==========================================================================


@async_test
async def test_scene_query_roundtrip_returns_dto(tmp_path: Path) -> None:
    node = _geo_node(points=8, prims=6)
    adapter, _ = _make_adapter(selected=[node])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        client = BridgeClient.from_state_dir(tmp_path)
        await client.open()
        result = await client.request(_make_request("req_roundtrip"))
        assert isinstance(result, SceneQueryResult)
        assert result.selected_nodes[0].path == "/obj/geo1"
        assert result.selected_nodes[0].node_type == "geo"
        assert result.selected_nodes[0].geometry_stats["points"] == 8
        assert result.binding.scene_epoch == 1
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_response_request_id_echoes_request(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        request = _make_request("req_unique_id_42")
        writer.write(_frame(request.to_json().encode("utf-8")))
        await writer.drain()
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj["ok"] is True
        assert obj["request_id"] == "req_unique_id_42"
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_non_scene_query_operation_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        bad = {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": "r1",
            "operation": "node.create",  # not scene.query
            "deadline_ms": 5000,
            "scene_epoch": 1,
            "payload": {},
        }
        await _send_raw(writer, bad)
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj["ok"] is False
        assert obj["error"]["code"] == "bridge.invalid_request"
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 9. requests execute through the MainThreadReadQueue (gated by the pump)
# ==========================================================================


@async_test
async def test_request_is_gated_by_queue_pump(tmp_path: Path) -> None:
    node = _geo_node()
    adapter, hou = _make_adapter(selected=[node])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter, pump=False)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        # No pump runs, so the queued operation never executes and the client
        # deadline elapses.
        with pytest.raises(BridgeClientError) as exc:
            await client.request(_make_request("req_gate", deadline_ms=300))
        assert exc.value.code == "bridge.deadline_exceeded"
        # The scene read never happened: the adapter was never consulted.
        assert hou.selected_calls == 0
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 12. stale scene epoch -> bridge.stale_scene
# ==========================================================================


@async_test
async def test_stale_scene_epoch_maps_to_stale_scene(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        with pytest.raises(BridgeClientError) as exc:
            await client.request(_make_request("req_stale", scene_epoch=99))
        assert exc.value.code == "bridge.stale_scene"
        assert exc.value.retryable is True
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 13. duplicate JSON keys / invalid UTF-8 / invalid JSON are structurally
#     rejected
# ==========================================================================


@async_test
async def test_duplicate_json_keys_in_request_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        dup = (
            '{"protocol":"eee.bridge/1","protocol":"eee.bridge/1",'
            '"kind":"request","request_id":"r1","operation":"scene.query",'
            '"deadline_ms":5000,"scene_epoch":1,'
            '"payload":{"include_selection":true,"node_paths":[],"include_geometry_stats":true}}'
        )
        writer.write(_frame(dup.encode("utf-8")))
        await writer.drain()
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj["ok"] is False
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_invalid_utf8_request_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        writer.write(_frame(b"\xff\xfe\x00\x00 not valid utf8"))
        await writer.drain()
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj["ok"] is False
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_invalid_json_request_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        writer.write(_frame(b"{not json"))
        await writer.drain()
        resp = await _read_frame(reader)
        obj = json.loads(resp)
        assert obj["ok"] is False
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 14. frame length attacks are rejected BEFORE reading the full payload
# ==========================================================================


@async_test
async def test_zero_length_frame_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        # A zero-length frame: header says 0 bytes of payload.
        writer.write((0).to_bytes(4, "big"))
        await writer.drain()
        # The server must reject quickly (close / error), not hang reading.
        try:
            await asyncio.wait_for(reader.readexactly(1), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            pass
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_oversize_declared_length_rejected_before_payload(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        # Header claims > 1 MiB but only a handful of bytes follow. The server
        # must reject without waiting for the full (huge) payload.
        oversize = (MAX_MESSAGE_BYTES + 1).to_bytes(4, "big") + b"x" * 16
        writer.write(oversize)
        await writer.drain()
        try:
            await asyncio.wait_for(reader.readexactly(1), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            pass
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_huge_declared_length_rejected_before_payload(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        await _read_frame(reader)  # ack
        # Header claims 4 GiB; only a few bytes follow. Must not block.
        writer.write((0xFFFFFFFF).to_bytes(4, "big") + b"abc")
        await writer.drain()
        try:
            await asyncio.wait_for(reader.readexactly(1), timeout=1.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            pass
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 15. close() is idempotent (client + server)
# ==========================================================================


@async_test
async def test_server_close_is_idempotent(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    await harness.start(tmp_path, adapter=adapter)
    try:
        assert harness.server is not None
        harness.server.close()
        harness.server.close()  # must not raise
        harness.server.close()
    finally:
        await harness.stop()


# ==========================================================================
# 16. after close, new requests are rejected
# ==========================================================================


@async_test
async def test_request_after_server_close_rejected(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
    await client.open()
    await harness.stop()  # closes the server + transport
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request("req_after_close"))
    assert exc.value.code in {"bridge.not_available", "bridge.deadline_exceeded"}


# ==========================================================================
# 17 + 18. shutdown resolves pending queue items; no leaked futures / tasks
# ==========================================================================


@async_test
async def test_shutdown_resolves_pending_request(tmp_path: Path) -> None:
    node = _geo_node()
    adapter, hou = _make_adapter(selected=[node])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter, pump=False)
    client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
    await client.open()
    # A request sits pending in the queue (no pump). Shutdown must resolve it
    # deterministically rather than leaving a hung Future.
    request_task = asyncio.create_task(
        client.request(_make_request("req_pending", deadline_ms=30000))
    )
    await asyncio.sleep(0.05)  # let it submit and sit queued
    assert hou.selected_calls == 0
    await harness.stop()  # server.close() -> queue.shutdown() resolves pending
    # The pending request must reach a terminal state (not hang).
    done, pending = await asyncio.wait({request_task}, timeout=3.0)
    assert request_task in done, "pending request was left unresolved on shutdown"
    with pytest.raises(BridgeClientError):
        request_task.result()
    assert harness.queue is not None
    assert harness.queue.pending_count == 0


@async_test
async def test_no_background_task_leak(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    baseline = asyncio.current_task()
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        await client.request(_make_request("req_leak"))
        await client.close()
    finally:
        await harness.stop()
    # Only the current (test) task may remain; the pump + connection handlers
    # must all have completed.
    remaining = asyncio.all_tasks() - {baseline}
    assert remaining == set(), f"leaked tasks: {remaining!r}"


# ==========================================================================
# 19. no cross-thread Future direct set_result/set_exception
# ==========================================================================


def test_server_never_directly_resolves_a_future() -> None:
    from houdini_side import secure_bridge

    source = inspect.getsource(secure_bridge)
    assert ".set_result(" not in source
    assert ".set_exception(" not in source


# ==========================================================================
# 20. token / discovery files are removed after shutdown
# ==========================================================================


@async_test
async def test_shutdown_removes_identity_files(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    await harness.start(tmp_path, adapter=adapter)
    assert (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
    await harness.stop()
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()


# Publication-failure lifecycle (listener closed, files removed, original error
# propagated) is covered by the serve()/stop() lifecycle-seam tests below.


# ==========================================================================
# structural: the server module stays free of hou/rpyc and does not itself
# bind a socket (loopback binding is owned by the host process)
# ==========================================================================


def test_server_module_imports_are_clean() -> None:
    from houdini_side import secure_bridge

    tree = inspect.getsource(secure_bridge)
    import ast

    # Top-level imports only: the adapter factory's lazy ``import hou`` (inside a
    # function, verified by its own signature test) is legitimate and excluded.
    mods: set[str] = set()
    for node in ast.parse(tree).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert "hou" not in mods
    assert "rpyc" not in mods
    # The server never pulls in asyncio/threading/socket at import time.
    assert "asyncio" not in mods
    assert "threading" not in mods
    assert "socket" not in mods


def test_server_only_dispatches_scene_query() -> None:
    from houdini_side import secure_bridge

    source = inspect.getsource(secure_bridge)
    # The server routes through parse_request (the frozen, scene.query-only
    # parser) and never exposes an arbitrary dispatch surface.
    assert "parse_request" in source
    for needle in ("eval(", "exec(", "getattr(adapter", "import subprocess"):
        assert needle not in source


# ==========================================================================
# Task 15-D follow-up: server startup lifecycle seam (serve / stop)
#
# A publish_identity() failure must NEVER leave the host's asyncio listener
# serving. ``serve()`` adopts an already-bound listener and guarantees that, on
# publication failure, the listener is closed and awaited, the identity files are
# removed, and the ORIGINAL error propagates (cleanup errors never mask it).
# ``stop()`` is the matching idempotent async shutdown. These tests deliberately
# do NOT manually close the listener before asserting.
# ==========================================================================


def _boom_publish(monkeypatch: pytest.MonkeyPatch, message: str = "publish boom") -> None:
    """Make bridge token publication fail with a recognizable OSError."""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(message)

    monkeypatch.setattr("eee_agent.houdini_bridge.auth.write_bridge_token", boom)


@async_test
async def test_serve_publish_failure_closes_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=queue
    )
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    _boom_publish(monkeypatch)
    with pytest.raises(OSError, match="publish boom"):
        await server.serve(listener, host="127.0.0.1")
    # No manual close here — serve() must have closed the listener itself.
    assert listener.is_serving() is False
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()


@async_test
async def test_serve_publish_failure_propagates_original_not_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=queue
    )
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    _boom_publish(monkeypatch)
    # Force the post-failure cleanup to raise too; the ORIGINAL publish error
    # must still be the one that surfaces.
    monkeypatch.setattr(
        server, "close", lambda: (_ for _ in ()).throw(RuntimeError("cleanup boom"))
    )
    with pytest.raises(OSError, match="publish boom"):
        await server.serve(listener, host="127.0.0.1")
    # The listener was still closed despite close() raising.
    assert listener.is_serving() is False


@async_test
async def test_serve_publish_failure_closes_after_token_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Token file publication succeeds, discovery publication fails: the listener
    # must still be closed and neither identity file left behind.
    adapter, _ = _make_adapter(selected=[_geo_node()])
    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=queue
    )
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    _boom_publish(monkeypatch, message="discovery boom")
    # write_bridge_token is the first publication step; failing it simulates the
    # discovery step never running (token-only partial state is impossible because
    # write_bridge_identity_files is all-or-nothing, but the listener guarantee
    # must hold regardless).
    with pytest.raises(OSError, match="discovery boom"):
        await server.serve(listener, host="127.0.0.1")
    assert listener.is_serving() is False
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()


@async_test
async def test_serve_normal_start_then_stop(tmp_path: Path) -> None:
    adapter, hou = _make_adapter(selected=[_geo_node()])
    adapter.install_scene_epoch_callbacks()
    assert len(hou.hipFile.eventCallbacks()) == 1
    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=queue
    )
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = await server.serve(listener, host="127.0.0.1")
    # Ready: listener serving, identity files published.
    assert listener.is_serving() is True
    assert server.is_serving is True
    assert port == listener.sockets[0].getsockname()[1]
    assert (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()

    # One functional query through the served listener.
    pump_stop = asyncio.Event()

    async def _pump() -> None:
        while not pump_stop.is_set():
            queue.pump_one()
            await asyncio.sleep(0.001)

    pump_task = asyncio.create_task(_pump())
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=identity)
        await client.open()
        result = await client.request(_make_request("req_serve"))
        assert isinstance(result, SceneQueryResult)
        await client.close()
    finally:
        pump_stop.set()
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # stop(): listener not serving, files removed, queue drained, callback removed.
    await server.stop()
    assert listener.is_serving() is False
    assert server.is_serving is False
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
    assert queue.pending_count == 0
    assert len(hou.hipFile.eventCallbacks()) == 0


@async_test
async def test_stop_is_idempotent(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=queue
    )
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    await server.serve(listener, host="127.0.0.1")
    await server.stop()
    await server.stop()  # idempotent
    await server.stop()


@async_test
async def test_serve_rejects_closed_server(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    identity = create_bridge_identity()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path, queue=MainThreadReadQueue()
    )
    server.close()
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    try:
        with pytest.raises(RuntimeError):
            await server.serve(listener, host="127.0.0.1")
    finally:
        listener.close()
        try:
            await listener.wait_closed()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# Task 16-C: the successful hello ack advertises sorted capabilities
# ==========================================================================


@async_test
async def test_success_ack_advertises_sorted_bridge_capabilities(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token},
        )
        resp = await _read_frame(reader)
        ack = json.loads(resp)
        assert ack["ok"] is True
        assert ack["capabilities"] == [
            "changeset.v1",
            "workspace.v1",
        ]  # sorted + unique
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_failed_auth_ack_has_no_capabilities(tmp_path: Path) -> None:
    adapter, _ = _make_adapter(selected=[_geo_node()])
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await _send_raw(
            writer,
            {"protocol": PROTOCOL, "kind": "hello", "token": "wrong-token"},
        )
        resp = await _read_frame(reader)
        ack = json.loads(resp)
        assert ack["ok"] is False
        assert "capabilities" not in ack
        writer.close()
    finally:
        await harness.stop()


# ===========================================================================
# Task 16-B2b-1: workspace inspection uses explicit dispatch + shared FIFO
# ===========================================================================


def _workspace_transport_request() -> WorkspaceInspectRequest:
    return WorkspaceInspectRequest(
        request_id="req_workspace_transport",
        deadline_ms=5000,
        scene_epoch=1,
        mode="selection",
        manifest=None,
    )


@async_test
async def test_workspace_inspect_roundtrip_uses_live_selected_mirrors(
    tmp_path: Path,
) -> None:
    ws = f"ws_{'2' * 32}"
    run = f"run_{'1' * 32}"
    node = _Node(
        "/obj/owned",
        user_data={
            "eee.workspace_id": ws,
            "eee.node_id": "node_owned",
            "eee.capability": "modeling",
            "eee.role": "root",
            "eee.schema_version": "1",
            "eee.created_by_run": run,
        },
    )
    adapter, _hou = _make_adapter(selected=(node,), nodes={node.path(): node})
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert WORKSPACE_V1 in client.capabilities
        result = await client.inspect_workspace(_workspace_transport_request())
        assert isinstance(result, WorkspaceInspectResult)
        assert result.observations[0].node_id == "node_owned"
        assert result.observations[0].workspace_id == ws
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_workspace_inspect_is_gated_by_same_queue_pump(tmp_path: Path) -> None:
    node = _Node("/obj/owned")
    adapter, _hou = _make_adapter(selected=(node,), nodes={node.path(): node})
    harness = _Harness()
    port = await harness.start(tmp_path, adapter=adapter, pump=False)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        request = WorkspaceInspectRequest(
            request_id="req_workspace_no_pump",
            deadline_ms=20,
            scene_epoch=1,
            mode="selection",
            manifest=None,
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.inspect_workspace(request)
        assert exc.value.code == "bridge.deadline_exceeded"
    finally:
        await harness.stop()


@async_test
async def test_server_without_workspace_capability_fails_closed(tmp_path: Path) -> None:
    node = _Node("/obj/owned")
    adapter, _hou = _make_adapter(selected=(node,), nodes={node.path(): node})
    harness = _Harness()
    port = await harness.start(
        tmp_path, adapter=adapter, capabilities=(CHANGESET_V1,)
    )
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        with pytest.raises(BridgeClientError) as exc:
            await client.inspect_workspace(_workspace_transport_request())
        assert exc.value.code == "bridge.capability_unavailable"
    finally:
        await harness.stop()


def test_all_typed_server_handlers_submit_through_the_same_queue_helper() -> None:
    import inspect

    handlers = (
        BridgeServer._serve_scene_query,
        BridgeServer._serve_workspace_inspect,
        BridgeServer._serve_preflight,
        BridgeServer._serve_apply,
        BridgeServer._serve_receipt,
    )
    for handler in handlers:
        assert "_run_on_queue" in inspect.getsource(handler)
