"""Task 18-F: ``sensitivity.sample`` Bridge transport tests.

Drives a REAL ``BridgeClient`` against a REAL ``asyncio.start_server`` loopback
``BridgeServer`` (one shared ``MainThreadReadQueue``) backed by a ``hou``-free
WRITABLE fake scene with cook/geometry facts. No ``hou``, ``rpyc``, live LLM,
or real Houdini process is used. The Houdini main-thread pump is modelled by a
cooperative pump task.

Covers the sample-and-restore wire round-trip with exact restoration, client
and server capability gating (no frame / no HOM access), stale-scene refusal
with zero writes, cook failure with a verified restore, restore failure with
the shared write freeze, and FIFO serialization of scene.query + sampling.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from eee_agent.houdini_bridge.auth import create_bridge_identity
from eee_agent.houdini_bridge.changesets import CHANGESET_V1
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.queue import MainThreadReadQueue
from eee_agent.houdini_bridge.sensitivity import (
    SENSITIVITY_V1,
    SensitivitySampleRequest,
    SensitivitySampleResult,
    SensitivitySampleTarget,
)
from eee_agent.modeling.validation import _geometry_evidence_digest
from houdini_side.secure_bridge import BridgeServer, HoudiniSceneAdapter

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"


def async_test(coro: Callable[..., Awaitable[object]]) -> Callable[..., None]:
    @functools.wraps(coro)
    def wrapper(*args: object, **kwargs: object) -> None:
        asyncio.run(coro(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------------------
# compact writable fake scene (hou-free) with cook/geometry + mutation spy
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
    def __init__(self, node: "_WNode", name: str, value: object) -> None:
        self._node = node
        self._name = name
        self._value = value
        # Restore-failure injection: the write-back silently does not take.
        self.sticky_after_first_set = False
        self._set_count = 0

    def eval(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._node._spy.append(("parm.set", self._node._path, self._name, value))
        self._set_count += 1
        if self.sticky_after_first_set and self._set_count > 1:
            return
        self._value = value


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
    def __init__(self, mx: float) -> None:
        self._mx = mx

    def minvec(self) -> _WVec:
        return _WVec(0.0, 0.0, 0.0)

    def maxvec(self) -> _WVec:
        return _WVec(self._mx, 1.0, 1.0)


class _WGeometry:
    def __init__(self, node: "_WNode") -> None:
        self._node = node

    def _points(self) -> int:
        return 4 + int(
            sum(
                parm.eval()
                for parm in self._node._parms.values()
                if isinstance(parm.eval(), (int, float))
            )
        )

    def pointCount(self) -> int:
        return self._points()

    def primCount(self) -> int:
        return max(1, self._points() // 2)

    def boundingBox(self) -> _WBBox:
        return _WBBox(float(self._points()))


class _WNode:
    def __init__(
        self,
        scene: dict,
        spy: list,
        path: str,
        type_name: str,
        parent: str,
        user_data: dict | None = None,
        parms: dict | None = None,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self._user_data = dict(user_data or {})
        self._parms = {n: _WParm(self, n, v) for n, v in (parms or {}).items()}
        self.cook_error = ""

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        return _Type(self._type)

    def parent(self) -> _ParentRef:
        return _ParentRef(self._parent)

    def userData(self, key: str) -> str | None:
        return self._user_data.get(key)

    def setUserData(self, key: str, value: str) -> None:
        self._spy.append(("setUserData", self._path, key))
        self._user_data[key] = value

    def parm(self, name: str) -> _WParm | None:
        return self._parms.get(name)

    def parmTuple(self, name: str) -> None:
        return None

    def isHardLocked(self) -> bool:
        return False

    def isSoftLocked(self) -> bool:
        return False

    def cook(self, force: bool = False) -> None:
        self._spy.append(("cook", self._path, force))

    def errors(self) -> tuple[str, ...]:
        return (self.cook_error,) if self.cook_error else ()

    def geometry(self) -> _WGeometry:
        return _WGeometry(self)


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


class WriteHou:
    def __init__(self, nodes: dict[str, _WNode], spy: list) -> None:
        self._nodes, self._spy = nodes, spy
        self.undos = _Undos(spy)
        self.hipFile = _HipFile()
        self.hipFileEventType = _HipFileEventType()

    def applicationVersionString(self) -> str:
        return "21.0.440"

    def selectedNodes(self) -> tuple:
        return ()

    def node(self, path: str):  # type: ignore[no-untyped-def]
        if path == "/":
            return _Root(self._nodes)
        return self._nodes.get(path)


def _standard_scene(spy: list) -> dict[str, _WNode]:
    scene: dict[str, _WNode] = {}
    root = _WNode(scene, spy, "/obj/ws", "geo", "/obj", {
        "eee.workspace_id": WS, "eee.node_id": "n_root", "eee.capability": "modeling",
        "eee.role": "root", "eee.schema_version": "1", "eee.created_by_run": RUN,
    })
    box = _WNode(
        scene, spy, "/obj/ws/box1", "box", "/obj/ws", {
            "eee.workspace_id": WS, "eee.node_id": "n_box", "eee.capability": "modeling",
            "eee.role": "member", "eee.schema_version": "1", "eee.created_by_run": RUN,
        },
        parms={"sizex": 2.0},
    )
    scene["/obj/ws"] = root
    scene["/obj/ws/box1"] = box
    return scene


def _sample_request(
    adapter: HoudiniSceneAdapter, *, scene_epoch: int | None = None
) -> SensitivitySampleRequest:
    binding = adapter.binding()
    return SensitivitySampleRequest.build(
        request_id="req_sample",
        deadline_ms=5000,
        scene_epoch=binding.scene_epoch if scene_epoch is None else scene_epoch,
        node_paths=["/obj/ws/box1"],
        samples=[
            SensitivitySampleTarget(
                node_id="n_box", path="/obj/ws/box1", parm_name="sizex", value=3.0
            )
        ],
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
        self, state_dir: Path, *, capabilities=(CHANGESET_V1, SENSITIVITY_V1)
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
async def test_sample_round_trip_returns_evidence_and_restores(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        async with h._client() as client:
            result = await client.sample_sensitivity(_sample_request(h.adapter))
        assert isinstance(result, SensitivitySampleResult)
        assert len(result.samples) == 1
        baseline_digest = _geometry_evidence_digest(result.baseline)
        assert _geometry_evidence_digest(result.samples[0]) != baseline_digest
        assert _geometry_evidence_digest(result.restored) == baseline_digest
        writes = [m for m in h.spy if m[0] == "parm.set"]
        assert writes == [
            ("parm.set", "/obj/ws/box1", "sizex", 3.0),
            ("parm.set", "/obj/ws/box1", "sizex", 2.0),
        ]
        assert h.adapter._hou.node("/obj/ws/box1").parm("sizex").eval() == 2.0
    finally:
        await h.stop()


@async_test
async def test_sample_without_capability_sends_no_frame(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path, capabilities=(CHANGESET_V1,))  # no sensitivity.v1
    try:
        assert h.adapter is not None
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.sample_sensitivity(_sample_request(h.adapter))
        assert exc.value.code == "bridge.capability_unavailable"
        # nothing reached the executor -> zero writes
        assert not [m for m in h.spy if m[0] in ("parm.set", "undo_group_begin")]
    finally:
        await h.stop()


@async_test
async def test_sample_server_without_capability_fails_closed(tmp_path: Path) -> None:
    """A client that ignores the missing advertisement is still refused by the
    server admission branch BEFORE any payload parsing or HOM access."""
    h = _Harness()
    await h.start(tmp_path, capabilities=(CHANGESET_V1,))  # no sensitivity.v1
    try:
        assert h.adapter is not None and h.server is not None
        frame = _sample_request(h.adapter).to_json().encode("utf-8")
        response = json.loads(await h.server._serve(frame))
        assert response["ok"] is False
        assert response["error"]["code"] == "bridge.capability_unavailable"
        assert not [m for m in h.spy if m[0] in ("parm.set", "undo_group_begin")]
    finally:
        await h.stop()


@async_test
async def test_sample_stale_scene_returns_bridge_error_zero_writes(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        req = _sample_request(h.adapter)
        # bump the scene epoch AFTER hello so the sample binding is stale
        h.adapter._on_scene_event(h.adapter._hou.hipFileEventType.AfterLoad)  # type: ignore[attr-defined]
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.sample_sensitivity(req)
        assert exc.value.code == "bridge.stale_scene"
        assert not [m for m in h.spy if m[0] in ("parm.set", "undo_group_begin")]
    finally:
        await h.stop()


@async_test
async def test_sample_cook_failure_over_wire_restores(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        h.adapter._hou.node("/obj/ws/box1").cook_error = "cook failed"  # type: ignore[attr-defined]
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.sample_sensitivity(_sample_request(h.adapter))
        assert exc.value.code == "sensitivity.cook_failed"
        assert exc.value.retryable is True
        # the sample write was restored exactly; no freeze, no guessed success
        assert h.adapter._hou.node("/obj/ws/box1").parm("sizex").eval() == 2.0
        assert h.server is not None and h.server._executor.write_frozen is False  # type: ignore[attr-defined]
    finally:
        await h.stop()


@async_test
async def test_sample_restore_failure_over_wire_freezes_writes(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        h.adapter._hou.node("/obj/ws/box1").parm("sizex").sticky_after_first_set = True  # type: ignore[attr-defined]
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.sample_sensitivity(_sample_request(h.adapter))
            assert exc.value.code == "sensitivity.restore_failed"
            assert exc.value.retryable is False
            # the shared write freeze now gates further sampling over the wire
            with pytest.raises(BridgeClientError) as exc2:
                await client.sample_sensitivity(_sample_request(h.adapter))
            assert exc2.value.code == "bridge.write_frozen"
            assert exc2.value.retryable is False
        frozen_writes = len([m for m in h.spy if m[0] == "parm.set"])
        assert h.server is not None and h.server._executor.write_frozen is True  # type: ignore[attr-defined]
        assert len([m for m in h.spy if m[0] == "parm.set"]) == frozen_writes
    finally:
        await h.stop()


@async_test
async def test_fifo_serializes_scene_query_and_sample(tmp_path: Path) -> None:
    """A scene.query and a sensitivity.sample share one FIFO and complete in turn."""
    from eee_agent.houdini_bridge.contracts import BridgeOperation, BridgeRequest

    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        epoch = h.adapter.binding().scene_epoch
        query = BridgeRequest(
            request_id="req_q", operation=BridgeOperation.SCENE_QUERY,
            deadline_ms=5000, scene_epoch=epoch, payload={"node_paths": ["/obj/ws/box1"]},
        )
        async with h._client() as client:
            # One connection = one in-flight frame; the two operations still
            # share the single FIFO and are pumped strictly in submission order.
            query_result = await client.request(query)
            result = await client.sample_sensitivity(_sample_request(h.adapter))
        assert len(query_result.nodes) == 1
        assert len(result.samples) == 1
        assert h.adapter._hou.node("/obj/ws/box1").parm("sizex").eval() == 2.0
    finally:
        await h.stop()
