from __future__ import annotations

import asyncio
import inspect
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from eee_agent.houdini_bridge.client import BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)
from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
)
from houdini_side.secure_bridge import HoudiniSceneAdapter
from houdini_side.secure_bridge_host import (
    SecureBridgeHost,
    SecureBridgeHostError,
    SelectionQueryError,
    query_selection,
    run_background_async,
)


def _binding(epoch: int = 3) -> SceneBinding:
    return SceneBinding(
        instance_id="hou:21.0.440:pid1",
        scene_epoch=epoch,
        hip_path="C:/project/test.hip",
        observed_revision="sha256:" + "a" * 64,
    )


def _result(epoch: int = 3) -> SceneQueryResult:
    node = SelectedNode(
        path="/obj/geo1",
        node_type="geo",
        parent_path="/obj",
        display_name="geo1",
        is_locked=False,
        geometry_stats={"points": 8, "primitives": 6},
    )
    return SceneQueryResult(
        binding=_binding(epoch),
        selected_nodes=(node,),
        nodes=(),
    )


class _FakeClient:
    def __init__(
        self,
        *,
        binding_epoch: int = 3,
        result_epoch: int = 3,
        request_error: BaseException | None = None,
    ) -> None:
        self.binding_epoch = binding_epoch
        self.result_epoch = result_epoch
        self.request_error = request_error
        self.inspect_requests = []
        self.scene_requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True

    async def inspect_workspace(self, request):
        self.inspect_requests.append(request)
        return SimpleNamespace(binding=_binding(self.binding_epoch))

    async def request(self, request):
        self.scene_requests.append(request)
        if self.request_error is not None:
            raise self.request_error
        return _result(self.result_epoch)


def test_query_selection_binds_then_queries_exact_epoch(tmp_path: Path) -> None:
    client = _FakeClient()

    async def scenario() -> SceneQueryResult:
        return await query_selection(
            tmp_path,
            client_factory=lambda _state: client,
        )

    result = asyncio.run(scenario())
    assert result.selected_nodes[0].path == "/obj/geo1"
    assert len(client.inspect_requests) == 1
    inspect_request = client.inspect_requests[0]
    assert inspect_request.mode == "selection"
    assert inspect_request.scene_epoch is None
    assert inspect_request.manifest is None
    assert len(client.scene_requests) == 1
    scene_request = client.scene_requests[0]
    assert scene_request.scene_epoch == 3
    assert scene_request.payload["include_selection"] is True
    assert scene_request.payload["include_geometry_stats"] is True
    assert scene_request.payload["node_paths"] == ()
    assert client.closed is True


def test_query_selection_retries_complete_cycle_once_on_stale(tmp_path: Path) -> None:
    stale = BridgeClientError(
        code="bridge.stale_scene",
        category="stale_scene",
        message_for_user="The scene changed.",
        retryable=True,
    )
    clients = [
        _FakeClient(request_error=stale),
        _FakeClient(binding_epoch=4, result_epoch=4),
    ]

    def factory(_state: Path):
        return clients.pop(0)

    result = asyncio.run(query_selection(tmp_path, client_factory=factory))
    assert result.binding.scene_epoch == 4
    assert clients == []


def test_query_selection_never_weakens_after_second_stale(tmp_path: Path) -> None:
    def factory(_state: Path):
        return _FakeClient(
            request_error=BridgeClientError(
                code="bridge.stale_scene",
                category="stale_scene",
                message_for_user="The scene changed.",
                retryable=True,
            )
        )

    with pytest.raises(SelectionQueryError) as exc:
        asyncio.run(query_selection(tmp_path, client_factory=factory))
    assert exc.value.code == "bridge.stale_scene"
    assert exc.value.retryable is True


def test_query_selection_rejects_binding_mismatch(tmp_path: Path) -> None:
    client = _FakeClient(binding_epoch=3, result_epoch=4)
    with pytest.raises(SelectionQueryError) as exc:
        asyncio.run(
            query_selection(tmp_path, client_factory=lambda _state: client)
        )
    assert exc.value.code == "bridge.binding_mismatch"


class _FakeUI:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def addEventLoopCallback(self, callback) -> None:
        self.callbacks.append(callback)

    def removeEventLoopCallback(self, callback) -> None:
        self.callbacks.remove(callback)


class _FakeAdapter:
    def __init__(self) -> None:
        self.installed = 0
        self.closed = 0

    def install_scene_epoch_callbacks(self) -> None:
        self.installed += 1

    def close(self) -> None:
        self.closed += 1


class _FakeQueue:
    def __init__(self) -> None:
        self.pumps = 0
        self.shutdowns = 0

    def pump_one(self) -> bool:
        self.pumps += 1
        return False

    def shutdown(self) -> None:
        self.shutdowns += 1


class _FakeListener:
    pass


class _FakeServer:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.handle_connection = object()
        self.serve_calls = []
        self.stops = 0

    async def serve(self, listener, *, host: str) -> int:
        self.serve_calls.append((listener, host))
        return 45678

    async def stop(self) -> None:
        self.stops += 1


def _host(tmp_path: Path):
    ui = _FakeUI()
    hou = SimpleNamespace(ui=ui)
    adapter = _FakeAdapter()
    queue = _FakeQueue()
    servers: list[_FakeServer] = []
    listener = _FakeListener()

    def server_factory(**kwargs):
        server = _FakeServer(**kwargs)
        servers.append(server)
        return server

    async def start_server_factory(handler, host, port):
        assert host == "127.0.0.1"
        assert port == 0
        return listener

    host = SecureBridgeHost(
        hou_module=hou,
        state_dir=tmp_path,
        adapter_factory=lambda: adapter,
        identity_factory=lambda: object(),
        queue_factory=lambda: queue,
        server_factory=server_factory,
        start_server_factory=start_server_factory,
        start_timeout=2,
        join_timeout=2,
    )
    return host, ui, adapter, queue, servers, listener


def test_secure_bridge_host_is_idempotent_and_pumps_on_owner_thread(
    tmp_path: Path,
) -> None:
    host, ui, adapter, queue, servers, listener = _host(tmp_path)
    owner = threading.get_ident()
    assert host.start() is host
    assert host.start() is host
    assert host.is_running is True
    assert host.port == 45678
    assert adapter.installed == 1
    assert len(ui.callbacks) == 1
    ui.callbacks[0]()
    assert queue.pumps == 1
    assert threading.get_ident() == owner
    assert len(servers) == 1
    assert servers[0].serve_calls == [(listener, "127.0.0.1")]

    host.stop()
    host.stop()
    assert host.is_running is False
    assert ui.callbacks == []
    assert queue.shutdowns >= 1
    assert adapter.closed >= 1
    assert servers[0].stops == 1


def test_secure_bridge_host_start_failure_cleans_main_thread_state(
    tmp_path: Path,
) -> None:
    ui = _FakeUI()
    adapter = _FakeAdapter()
    queue = _FakeQueue()
    server = _FakeServer()

    async def failing_start_server(handler, host, port):
        raise OSError("bind failed")

    host = SecureBridgeHost(
        hou_module=SimpleNamespace(ui=ui),
        state_dir=tmp_path,
        adapter_factory=lambda: adapter,
        identity_factory=lambda: object(),
        queue_factory=lambda: queue,
        server_factory=lambda **kwargs: server,
        start_server_factory=failing_start_server,
        start_timeout=2,
        join_timeout=2,
    )
    with pytest.raises(SecureBridgeHostError):
        host.start()
    assert host.is_running is False
    assert ui.callbacks == []
    assert adapter.closed >= 1
    assert server.stops == 1


def test_host_bypasses_process_event_loop_policy_in_worker_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_policy_loop():
        raise RuntimeError("Houdini haio policy must not be consulted")

    monkeypatch.setattr(asyncio, "new_event_loop", forbidden_policy_loop)
    host, _ui, _adapter, _queue, _servers, _listener = _host(tmp_path)
    try:
        host.start()
        assert host.is_running is True
    finally:
        host.stop()


def test_background_async_runner_bypasses_asyncio_run_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        asyncio,
        "run",
        lambda _coro: (_ for _ in ()).throw(
            RuntimeError("asyncio.run must not be used")
        ),
    )
    monkeypatch.setattr(
        asyncio,
        "new_event_loop",
        lambda: (_ for _ in ()).throw(
            RuntimeError("Houdini haio policy must not be consulted")
        ),
    )

    async def operation() -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        return type(loop).__module__, threading.current_thread().name

    outcome: dict[str, object] = {}

    def run_operation() -> None:
        outcome["value"] = run_background_async(operation)

    thread = threading.Thread(target=run_operation, name="haio-policy-probe")
    thread.start()
    thread.join(2)
    assert thread.is_alive() is False
    loop_module, thread_name = outcome["value"]
    assert loop_module.startswith("asyncio.")
    assert thread_name == "haio-policy-probe"


def test_host_source_uses_one_loopback_listener_and_houdini_idle_pump() -> None:
    source = inspect.getsource(
        __import__(
            "houdini_side.secure_bridge_host",
            fromlist=["SecureBridgeHost"],
        )
    )
    assert 'target=self._thread_main' in source
    assert "SelectorEventLoop" in source
    assert "addEventLoopCallback(self._pump)" in source
    assert "MainThreadReadQueue" in source
    assert '_HOST = "127.0.0.1"' in source
    assert "rpyc" not in source
    assert "hrpyc" not in source


class _NodeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Vec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._values = (x, y, z)

    def x(self) -> float:
        return self._values[0]

    def y(self) -> float:
        return self._values[1]

    def z(self) -> float:
        return self._values[2]


class _Geometry:
    def pointCount(self) -> int:
        return 8

    def primCount(self) -> int:
        return 6

    def boundingBox(self):
        return SimpleNamespace(
            minvec=lambda: _Vec(-1, -1, -1),
            maxvec=lambda: _Vec(1, 1, 1),
        )


class _SceneNode:
    def __init__(
        self,
        path: str,
        node_type: str,
        *,
        parent=None,
        geometry: bool = False,
    ) -> None:
        self._path = path
        self._type = node_type
        self._parent = parent
        self._children: list[_SceneNode] = []
        self._geometry = geometry
        if parent is not None:
            parent._children.append(self)

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1] or "/"

    def type(self):
        return _NodeType(self._type)

    def parent(self):
        return self._parent

    def children(self) -> tuple:
        return tuple(self._children)

    def userData(self, _key: str):
        return None

    def isHardLocked(self) -> bool:
        return False

    def isSoftLocked(self) -> bool:
        return False

    def geometry(self):
        if not self._geometry:
            raise RuntimeError("no geometry")
        return _Geometry()


class _HipFile:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def name(self) -> str:
        return "C:/project/panel.hip"

    def addEventCallback(self, callback) -> None:
        self.callbacks.append(callback)

    def removeEventCallback(self, callback) -> None:
        self.callbacks.remove(callback)


class _RealFakeHou:
    def __init__(self) -> None:
        self.ui = _FakeUI()
        self.hipFile = _HipFile()
        self.hipFileEventType = SimpleNamespace(
            AfterLoad="AfterLoad",
            AfterClear="AfterClear",
        )
        self.root = _SceneNode("/", "root")
        self.obj = _SceneNode("/obj", "obj", parent=self.root)
        self.geo = _SceneNode(
            "/obj/geo1",
            "geo",
            parent=self.obj,
            geometry=True,
        )
        self._nodes = {
            "/": self.root,
            "/obj": self.obj,
            "/obj/geo1": self.geo,
        }

    def applicationVersionString(self) -> str:
        return "21.0.440"

    def selectedNodes(self) -> tuple:
        return (self.geo,)

    def node(self, path: str):
        return self._nodes.get(path)


def test_host_real_transport_roundtrips_bound_selection_and_cleans_files(
    tmp_path: Path,
) -> None:
    hou = _RealFakeHou()
    adapter = HoudiniSceneAdapter(hou)
    host = SecureBridgeHost(
        hou_module=hou,
        state_dir=tmp_path,
        adapter_factory=lambda: adapter,
        start_timeout=5,
        join_timeout=5,
    )
    host.start()
    assert (tmp_path / BRIDGE_DISCOVERY_FILENAME).is_file()
    assert (tmp_path / BRIDGE_TOKEN_FILENAME).is_file()

    outcome: dict[str, object] = {}

    def run_query() -> None:
        try:
            outcome["result"] = asyncio.run(query_selection(tmp_path))
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run_query, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        for callback in tuple(hou.ui.callbacks):
            callback()
        time.sleep(0.001)
    thread.join(1)
    try:
        assert "error" not in outcome
        result = outcome["result"]
        assert isinstance(result, SceneQueryResult)
        assert result.binding.scene_epoch == 1
        assert result.binding.hip_path == "C:/project/panel.hip"
        assert result.selected_nodes[0].path == "/obj/geo1"
        assert result.selected_nodes[0].geometry_stats["points"] == 8
    finally:
        host.stop()

    assert hou.ui.callbacks == []
    assert hou.hipFile.callbacks == []
    assert not (tmp_path / BRIDGE_DISCOVERY_FILENAME).exists()
    assert not (tmp_path / BRIDGE_TOKEN_FILENAME).exists()
