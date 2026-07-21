"""Task 19-A: ``capture.capture`` Bridge transport tests.

Drives a REAL ``BridgeClient`` against a REAL ``asyncio.start_server`` loopback
``BridgeServer`` (one shared ``MainThreadReadQueue``) backed by a ``hou``-free
fake scene whose Flipbook ROP writes PNG bytes into the Runtime-owned target
directory. No ``hou``, ``rpyc``, live LLM, or real Houdini process is used.
The Houdini main-thread pump is modelled by a cooperative pump task.

Covers the capture wire round-trip (verified hash, atomic rename, framing
report, temp scope destroyed), client and server capability gating (no frame
/ no HOM access), stale-scene refusal with zero scene changes, and a render
failure classified over the wire with the temp scope cleaned.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from eee_agent.houdini_bridge.auth import create_bridge_identity
from eee_agent.houdini_bridge.capture import (
    CAPTURE_V1,
    CaptureRequest,
    CaptureResult,
)
from eee_agent.houdini_bridge.changesets import CHANGESET_V1
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.queue import MainThreadReadQueue
from houdini_side.secure_bridge import BridgeServer, HoudiniSceneAdapter

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
ART = f"art_{'a' * 32}"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FAKE_PNG = _PNG_MAGIC + b"\x00\x00\x00\rIHDR" + bytes(range(48))


def async_test(coro: Callable[..., Awaitable[object]]) -> Callable[..., None]:
    @functools.wraps(coro)
    def wrapper(*args: object, **kwargs: object) -> None:
        asyncio.run(coro(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------------------
# compact capture-capable fake scene (hou-free) with mutation spy
# --------------------------------------------------------------------------


class _Type:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _ParentRef:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _WParm:
    def __init__(self, node: "_WNode", name: str) -> None:
        self._node = node
        self._name = name
        self._value: object = None

    def set(self, value: object) -> None:
        self._node._spy.append(("parm.set", self._node._path, self._name, value))
        self._value = value

    def eval(self) -> object:
        return self._value


class _WVec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._v = (x, y, z)

    def x(self) -> float:
        return self._v[0]

    def y(self) -> float:
        return self._v[1]

    def z(self) -> float:
        return self._v[2]


class _WBBox:
    def __init__(self, mn: tuple, mx: tuple) -> None:
        self._mn, self._mx = mn, mx

    def minvec(self) -> _WVec:
        return _WVec(*self._mn)

    def maxvec(self) -> _WVec:
        return _WVec(*self._mx)


class _WGeometry:
    def __init__(self, node: "_WNode") -> None:
        self._node = node

    def boundingBox(self) -> _WBBox:
        if self._node.bbox is None:
            raise RuntimeError("no geometry")
        return _WBBox(*self._node.bbox)


class _WNode:
    def __init__(
        self,
        scene: dict,
        spy: list,
        path: str,
        type_name: str,
        parent: str,
        bbox: tuple | None = None,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self.bbox = bbox
        self.cook_error = ""
        self.render_raise = False
        self._parms: dict[str, _WParm] = {}

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        return _Type(self._type)

    def parent(self) -> _ParentRef:
        return _ParentRef(self._parent)

    def userData(self, key: str) -> None:
        return None

    def parm(self, name: str) -> _WParm | None:
        if name not in self._parms:
            self._parms[name] = _WParm(self, name)
        return self._parms[name]

    def parmTuple(self, name: str) -> _WParm | None:
        key = name + "#tuple"
        if key not in self._parms:
            self._parms[key] = _WParm(self, name)
        return self._parms[key]

    def setParmTransform(self, matrix: object) -> None:
        self._spy.append(("setParmTransform", self._path, matrix))

    def cook(self, force: bool = False) -> None:
        self._spy.append(("cook", self._path, force))

    def errors(self) -> tuple[str, ...]:
        return (self.cook_error,) if self.cook_error else ()

    def geometry(self) -> _WGeometry:
        return _WGeometry(self)

    def render(self) -> None:
        self._spy.append(("render", self._path))
        if self.render_raise:
            raise RuntimeError("render crashed")
        Path(str(self._parms["picture"].eval())).write_bytes(_FAKE_PNG)

    def destroy(self) -> None:
        self._spy.append(("destroy", self._path))
        self._scene.pop(self._path, None)

    def createNode(self, type_name: str, name: str) -> "_WNode":
        self._spy.append(("createNode", self._path, type_name, name))
        child = _WNode(self._scene, self._spy, f"{self._path}/{name}", type_name, self._path)
        self._scene[child._path] = child
        return child


class _Root:
    def __init__(self, nodes: dict[str, _WNode]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple:
        return tuple(self._nodes.values())


class _Undos:
    def __init__(self, spy: list) -> None:
        self._spy = spy

    def group(self, label: str):  # type: ignore[no-untyped-def]
        spy = self._spy

        @contextlib.contextmanager
        def _g():
            spy.append(("undo_group_begin", label))
            try:
                yield
            finally:
                spy.append(("undo_group_end", label))

        return _g()


class _HipFile:
    def __init__(self) -> None:
        self._callbacks: list = []

    def name(self) -> str:
        return ""

    def addEventCallback(self, cb: object) -> None:
        self._callbacks.append(cb)

    def removeEventCallback(self, cb: object) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)


class _HipFileEventType:
    AfterClear = "AfterClear"
    AfterLoad = "AfterLoad"


class _Matrix4:
    def __init__(self, rows: object) -> None:
        self.rows = rows


class WriteHou:
    def __init__(self, nodes: dict[str, _WNode], spy: list) -> None:
        self._nodes, self._spy = nodes, spy
        self.undos = _Undos(spy)
        self.hipFile = _HipFile()
        self.hipFileEventType = _HipFileEventType()
        self.Matrix4 = _Matrix4

    def applicationVersionString(self) -> str:
        return "22.0.368"

    def selectedNodes(self) -> tuple:
        return ()

    def node(self, path: str):  # type: ignore[no-untyped-def]
        if path == "/":
            return _Root(self._nodes)
        return self._nodes.get(path)


def _standard_scene(spy: list) -> dict[str, _WNode]:
    scene: dict[str, _WNode] = {}
    scene["/obj"] = _WNode(scene, spy, "/obj", "obj", "/")
    scene["/out"] = _WNode(scene, spy, "/out", "out", "/")
    scene["/obj/ws"] = _WNode(
        scene, spy, "/obj/ws", "geo", "/obj",
        bbox=((0.0, 0.0, 0.0), (2.0, 1.0, 1.0)),
    )
    scene["/obj/ws/box1"] = _WNode(
        scene, spy, "/obj/ws/box1", "box", "/obj/ws",
        bbox=((0.0, 0.0, 0.0), (2.0, 1.0, 1.0)),
    )
    return scene


def _capture_request(
    adapter: HoudiniSceneAdapter, target_dir: Path, *, scene_epoch: int | None = None
) -> CaptureRequest:
    binding = adapter.binding()
    return CaptureRequest.build(
        request_id="req_capture",
        deadline_ms=5000,
        scene_epoch=binding.scene_epoch if scene_epoch is None else scene_epoch,
        node_paths=["/obj/ws/box1"],
        target_dir=str(target_dir),
        artifact_id=ART,
    )


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------


class _Harness:
    def __init__(self) -> None:
        self.spy: list = []
        self.adapter: HoudiniSceneAdapter | None = None
        self.server: BridgeServer | None = None
        self.queue: MainThreadReadQueue | None = None
        self.port = 0
        self.identity = None
        self._pump_task: asyncio.Task | None = None
        self._pump_stop: asyncio.Event | None = None

    async def start(
        self, state_dir: Path, *, capabilities=(CAPTURE_V1, CHANGESET_V1)
    ) -> None:
        scene = _standard_scene(self.spy)
        hou = WriteHou(scene, self.spy)
        self.adapter = HoudiniSceneAdapter(hou)
        self.queue = MainThreadReadQueue()
        self.identity = create_bridge_identity()
        self.server = BridgeServer(
            adapter=self.adapter, identity=self.identity, state_dir=state_dir,
            queue=self.queue, capabilities=capabilities,
        )
        aio = await asyncio.start_server(self.server.handle_connection, "127.0.0.1", 0)
        self.port = await self.server.serve(aio, host="127.0.0.1")
        self._pump_stop = asyncio.Event()

        async def _pump() -> None:
            assert self.queue is not None and self._pump_stop is not None
            while not self._pump_stop.is_set():
                self.queue.pump_one()
                await asyncio.sleep(0.001)

        self._pump_task = asyncio.create_task(_pump())

    async def stop(self) -> None:
        if self._pump_stop is not None:
            self._pump_stop.set()
        if self._pump_task is not None:
            await asyncio.sleep(0.01)
            self._pump_task.cancel()
        if self.server is not None:
            await self.server.stop()

    def _client(self) -> BridgeClient:
        return BridgeClient(
            host="127.0.0.1", port=self.port, identity=self.identity,  # type: ignore[arg-type]
            transport_factory=None,
        )


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


@async_test
async def test_capture_round_trip_returns_reference_and_cleans_scope(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    target = tmp_path / "artifacts"
    target.mkdir()
    try:
        assert h.adapter is not None
        async with h._client() as client:
            result = await client.capture(_capture_request(h.adapter, target))
        assert isinstance(result, CaptureResult)
        final = target / f"{ART}.png"
        payload = final.read_bytes()
        assert payload == _FAKE_PNG
        assert result.sha256 == hashlib.sha256(payload).hexdigest()
        assert result.size_bytes == len(payload)
        assert result.relative_path == f"{ART}.png"
        assert result.media_type == "image/png"
        framing = result.framing
        assert 0 <= framing.adjustments_used <= 2
        assert 0.72 <= framing.longest_axis_ratio <= 0.84
        assert not (target / f"{ART}.png.tmp").exists()
        # The owned temp scope was destroyed over the wire.
        assert not [p for p in h.adapter._hou._nodes if "eee_capture_" in p]  # type: ignore[attr-defined]
        destroys = [m for m in h.spy if m[0] == "destroy"]
        assert len(destroys) == 2
    finally:
        await h.stop()


@async_test
async def test_capture_without_capability_sends_no_frame(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path, capabilities=(CHANGESET_V1,))  # no capture.v1
    try:
        assert h.adapter is not None
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.capture(_capture_request(h.adapter, tmp_path))
        assert exc.value.code == "bridge.capability_unavailable"
        # nothing reached the executor -> zero scene changes
        assert not [m for m in h.spy if m[0] in ("createNode", "render", "cook")]
    finally:
        await h.stop()


@async_test
async def test_capture_server_without_capability_fails_closed(tmp_path: Path) -> None:
    """A client that ignores the missing advertisement is still refused by the
    server admission branch BEFORE any payload parsing or HOM access."""
    h = _Harness()
    await h.start(tmp_path, capabilities=(CHANGESET_V1,))  # no capture.v1
    try:
        assert h.adapter is not None and h.server is not None
        frame = _capture_request(h.adapter, tmp_path).to_json().encode("utf-8")
        response = json.loads(await h.server._serve(frame))
        assert response["ok"] is False
        assert response["error"]["code"] == "bridge.capability_unavailable"
        assert not [m for m in h.spy if m[0] in ("createNode", "render", "cook")]
    finally:
        await h.stop()


@async_test
async def test_capture_stale_scene_returns_bridge_error_zero_changes(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        req = _capture_request(h.adapter, tmp_path)
        # bump the scene epoch AFTER hello so the capture binding is stale
        h.adapter._on_scene_event(h.adapter._hou.hipFileEventType.AfterLoad)  # type: ignore[attr-defined]
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.capture(req)
        assert exc.value.code == "bridge.stale_scene"
        assert not [m for m in h.spy if m[0] in ("createNode", "render")]
    finally:
        await h.stop()


@async_test
async def test_capture_render_failure_over_wire_cleans_scope(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        out = h.adapter._hou.node("/out")  # type: ignore[attr-defined]
        original_create = out.createNode

        def _create(type_name: str, name: str) -> _WNode:
            node = original_create(type_name, name)
            node.render_raise = True
            return node

        out.createNode = _create
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.capture(_capture_request(h.adapter, tmp_path))
        assert exc.value.code == "capture.render_failed"
        assert exc.value.retryable is True
        # The temp scope was still destroyed; no capture file was left behind.
        assert not [p for p in h.adapter._hou._nodes if "eee_capture_" in p]  # type: ignore[attr-defined]
        assert not (tmp_path / f"{ART}.png").exists()
        assert not (tmp_path / f"{ART}.png.tmp").exists()
    finally:
        await h.stop()
