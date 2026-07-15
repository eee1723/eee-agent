"""Task 16-C: Bridge preflight capability, dispatch, and read-only facts.

Drives a REAL ``BridgeClient`` against a REAL ``asyncio.start_server`` loopback
server (``BridgeServer.handle_connection``) with a REAL ``MainThreadReadQueue``
and a ``hou``-free fake scene (enriched with mirrored ownership keys, parms,
wires, and a write spy). No ``hou``, ``rpyc``, live LLM, or real Houdini
process is exercised. The Houdini main-thread pump is modelled by a cooperative
pump task (the fake adapter performs no real HOM read).
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from eee_agent.changesets import (
    ChangeSet,
    CheckpointPlan,
    ConnectInput,
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    WorkspaceManifest,
)
from eee_agent.houdini_bridge.auth import create_bridge_identity
from eee_agent.houdini_bridge.changesets import (
    CHANGESET_V1,
    PreflightRequest,
    PreflightResult,
    parse_preflight_response,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    PROTOCOL,
    BridgeRequest,
    SceneBinding,
    SceneQueryResult,
)
from eee_agent.houdini_bridge.queue import MainThreadReadQueue
from houdini_side.changeset_executor import ChangeSetPreflightAdapter
from houdini_side.secure_bridge import BridgeServer, HoudiniSceneAdapter

# --------------------------------------------------------------------------
# constants + async helper
# --------------------------------------------------------------------------

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
REVISION = "a" * 64


def async_test(coro: Callable[..., Awaitable[object]]) -> Callable[..., None]:
    @functools.wraps(coro)
    def wrapper(*args: object, **kwargs: object) -> None:
        asyncio.run(coro(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------------------
# fake scene (hou-free) with mirrored ownership keys, parms, wires, write spy
# --------------------------------------------------------------------------


class _FakeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeParent:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _FakeParm:
    def __init__(self, value: object) -> None:
        self._value = value

    def eval(self) -> object:
        return self._value


class _FakeParmTuple:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = tuple(values)

    def size(self) -> int:
        return len(self._values)

    def eval(self) -> tuple[object, ...]:
        return tuple(self._values)


class _FakeConn:
    def __init__(self, output_node: _FakeNode, output_index: int, input_index: int) -> None:
        self._out = output_node
        self._oi = output_index
        self._ii = input_index

    def outputNode(self) -> _FakeNode:
        return self._out

    def outputIndex(self) -> int:
        return self._oi

    def inputIndex(self) -> int:
        return self._ii


class _FakeNode:
    """A fake Houdini node.

    Only the read surface the preflight adapter uses is implemented. Any access
    to an undefined attribute returns a callable that records a write into the
    shared ``spy`` list — so a mutation call anywhere in preflight is observable.
    """

    def __init__(
        self,
        path: str,
        type_name: str = "geo",
        parent: str = "/obj",
        *,
        user_data: dict[str, str] | None = None,
        parms: dict[str, _FakeParm] | None = None,
        parm_tuples: dict[str, _FakeParmTuple] | None = None,
        connections: list[_FakeConn] | None = None,
        hard_locked: bool = False,
        soft_locked: bool = False,
        spy: list | None = None,
    ) -> None:
        self.path_ = path
        self.type_name = type_name
        self.parent_path = parent
        self.user_data = user_data or {}
        self.parms = parms or {}
        self.parm_tuples = parm_tuples or {}
        self.connections = connections or []
        self.hard_locked = hard_locked
        self.soft_locked = soft_locked
        self.spy = spy

    def path(self) -> str:
        return self.path_

    def name(self) -> str:
        return self.path_.rsplit("/", 1)[-1]

    def type(self) -> _FakeType:
        return _FakeType(self.type_name)

    def parent(self) -> _FakeParent:
        return _FakeParent(self.parent_path)

    def isHardLocked(self) -> bool:
        return self.hard_locked

    def isSoftLocked(self) -> bool:
        return self.soft_locked

    def userData(self, key: str) -> str | None:
        return self.user_data.get(key)

    def parm(self, name: str) -> _FakeParm | None:
        return self.parms.get(name)

    def parmTuple(self, name: str) -> _FakeParmTuple | None:
        return self.parm_tuples.get(name)

    def inputConnections(self) -> list[_FakeConn]:
        return list(self.connections)

    def __getattr__(self, attr: str) -> object:
        spy = self.__dict__.get("spy")

        def _write(*_args: object, **_kwargs: object) -> None:
            if spy is not None:
                spy.append(("write", self.__dict__.get("path_", "?"), attr))

        return _write


class _HipFile:
    def __init__(self, name: str = "") -> None:
        self._name = name
        self._callbacks: list[Callable[[object], None]] = []

    def name(self) -> str:
        return self._name

    def addEventCallback(self, cb: Callable[[object], None]) -> None:
        self._callbacks.append(cb)

    def removeEventCallback(self, cb: Callable[[object], None]) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)


class _HipFileEventType:
    AfterClear = "AfterClear"
    AfterLoad = "AfterLoad"


class _FakeSceneRoot:
    """Synthetic scene root exposing a bounded read-only node enumeration.

    The resolver enumerates the scene once via ``hou.node("/").allSubChildren()``
    to index nodes by their mirrored stable id. This root returns every fake node
    in a single pass; it has no write surface (no ``__getattr__`` recorder), so
    enumeration never registers as a mutation.
    """

    def __init__(self, nodes: dict[str, _FakeNode]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple[_FakeNode, ...]:
        return tuple(self._nodes.values())


class FakeHou:
    def __init__(
        self,
        *,
        nodes: dict[str, _FakeNode] | None = None,
        version: str = "21.0.440",
        hip_name: str = "",
        spy: list | None = None,
    ) -> None:
        self._nodes = nodes or {}
        self._version = version
        self.hipFile = _HipFile(hip_name)
        self.hipFileEventType = _HipFileEventType()
        self.spy = spy

    def applicationVersionString(self) -> str:
        return self._version

    def selectedNodes(self) -> tuple:
        return ()

    def node(self, path: str) -> _FakeNode | _FakeSceneRoot | None:
        # The scene root exposes a bounded read-only enumeration used by the
        # identity resolver; it is not a writable fake node.
        if path == "/":
            return _FakeSceneRoot(self._nodes)
        n = self._nodes.get(path)
        if n is not None:
            # Attach the spy so writes on looked-up nodes are recorded.
            n.spy = self.spy
        return n

    def __getattr__(self, attr: str) -> object:
        spy = self.__dict__.get("spy")

        def _write(*_args: object, **_kwargs: object) -> None:
            if spy is not None:
                spy.append(("write", "hou", attr))

        return _write


def _owned(
    node_id: str,
    path: str,
    node_type: str,
    parent: str,
    capability: str = "modeling",
    role: str = "member",
) -> OwnedNodeRef:
    return OwnedNodeRef(
        node_id=node_id,
        path=path,
        node_type=node_type,
        parent_path=parent,
        capability=capability,
        role=role,
    )


def _mirror(node_id: str, capability: str = "modeling", role: str = "member") -> dict[str, str]:
    return {
        "eee.workspace_id": WS,
        "eee.node_id": node_id,
        "eee.capability": capability,
        "eee.role": role,
        "eee.schema_version": "1",
        "eee.created_by_run": RUN,
    }


def _noderef(
    node_id: str | None,
    path: str,
    expected_type: str,
    *,
    workspace_id: str | None = WS,
) -> NodeRef:
    return NodeRef(
        node_id=node_id,
        path=path,
        expected_type=expected_type,
        expected_workspace_id=workspace_id,
    )


def _standard_nodes(spy: list) -> dict[str, _FakeNode]:
    """A workspace with a root, a child geo (parm + wire), and a source."""
    root = _FakeNode(
        "/obj/ws",
        type_name="subnet",
        parent="/obj",
        user_data=_mirror("n_root", role="root"),
        spy=spy,
    )
    src = _FakeNode(
        "/obj/ws/src1",
        type_name="xform",
        parent="/obj/ws",
        user_data=_mirror("n_src"),
        spy=spy,
    )
    child = _FakeNode(
        "/obj/ws/geo1",
        type_name="geo",
        parent="/obj/ws",
        user_data=_mirror("n_child"),
        parms={"tx": _FakeParm(0)},
        parm_tuples={"t": _FakeParmTuple((0.0, 1.0, 0.0))},
        connections=[_FakeConn(src, 0, 0)],
        spy=spy,
    )
    return {"/obj/ws": root, "/obj/ws/geo1": child, "/obj/ws/src1": src}


def _standard_manifest(binding_instance: str, binding_epoch: int) -> WorkspaceManifest:
    root = _owned("n_root", "/obj/ws", "subnet", "/obj", role="root")
    child = _owned("n_child", "/obj/ws/geo1", "geo", "/obj/ws")
    src = _owned("n_src", "/obj/ws/src1", "xform", "/obj/ws")
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id=binding_instance,
        scene_epoch=binding_epoch,
        roots=[root],
        nodes=[root, child, src],
        created_by_run=RUN,
        updated_at=NOW,
    )


def _risk(
    operations: tuple[object, ...],
    affected_paths: tuple[str, ...],
    *,
    touches_external: bool = False,
) -> RiskSummary:
    effects = tuple(sorted({op.effect.value for op in operations}))  # type: ignore[attr-defined]
    return RiskSummary(
        touches_external_nodes=touches_external,
        changes_wiring=("wire.connect" in effects),
        requires_backup=False,
        operation_count=len(operations),
        effect_names=effects,
        affected_paths=affected_paths,
    )


def _changeset(
    binding,
    operations: tuple[object, ...],
    affected: tuple[NodeRef, ...],
    *,
    preconditions: tuple = (),
    read_deps: tuple = (),
    workspace_id: str | None = WS,
    permission: PermissionMode = PermissionMode.OWNED_WORKSPACE,
    scoped: tuple[str, ...] = (),
    affected_paths: tuple[str, ...] | None = None,
) -> ChangeSet:
    paths = affected_paths if affected_paths is not None else tuple(sorted({a.path for a in affected}))
    return ChangeSet(
        change_id=f"chg_{'4' * 32}",
        session_id=SES,
        run_id=RUN,
        scene_binding=binding,
        workspace_id=workspace_id,
        base_revision=REVISION,
        required_permission=permission,
        scoped_node_ids=scoped,
        operations=operations,
        affected_nodes=affected,
        read_dependencies=read_deps,
        preconditions=preconditions,
        expected_postconditions=(),
        risk_summary=_risk(operations, paths),
        checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
        created_at=NOW,
    )


def _preflight_for(
    adapter: HoudiniSceneAdapter,
    *,
    operations: tuple[object, ...],
    affected: tuple[NodeRef, ...],
    preconditions: tuple = (),
    read_deps: tuple = (),
    workspace_id: str | None = WS,
    permission: PermissionMode = PermissionMode.OWNED_WORKSPACE,
    scoped: tuple[str, ...] = (),
    affected_paths: tuple[str, ...] | None = None,
    request_id: str = "req_preflight",
    deadline_ms: int = 5000,
    with_workspace: bool = True,
) -> PreflightRequest:
    binding = adapter.binding()
    cs = _changeset(
        binding,
        operations,
        affected,
        preconditions=preconditions,
        read_deps=read_deps,
        workspace_id=workspace_id,
        permission=permission,
        scoped=scoped,
        affected_paths=affected_paths,
    )
    workspace = _standard_manifest(binding.instance_id, binding.scene_epoch) if with_workspace else None
    return PreflightRequest.build(
        request_id=request_id,
        deadline_ms=deadline_ms,
        scene_epoch=binding.scene_epoch,
        changeset=cs,
        workspace=workspace,
    )


# --------------------------------------------------------------------------
# harness (loopback server + cooperative pump + write spy)
# --------------------------------------------------------------------------


class _Harness:
    def __init__(self) -> None:
        self.spy: list = []
        self.server: BridgeServer | None = None
        self.queue: MainThreadReadQueue | None = None
        self.adapter: HoudiniSceneAdapter | None = None
        self.port: int = 0
        self.identity = None
        self._pump_task: asyncio.Task | None = None
        self._pump_stop: asyncio.Event | None = None

    def make_hou(self, nodes: dict[str, _FakeNode] | None = None) -> FakeHou:
        if nodes is None:
            nodes = _standard_nodes(self.spy)
        return FakeHou(nodes=nodes, spy=self.spy)

    async def start(
        self,
        state_dir: Path,
        *,
        nodes: dict[str, _FakeNode] | None = None,
        capabilities: tuple[str, ...] = (CHANGESET_V1,),
        pump: bool = True,
    ) -> int:
        hou = self.make_hou(nodes)
        self.adapter = HoudiniSceneAdapter(hou)
        self.queue = MainThreadReadQueue()
        self.identity = create_bridge_identity()
        self.server = BridgeServer(
            adapter=self.adapter,
            identity=self.identity,
            state_dir=state_dir,
            queue=self.queue,
            capabilities=capabilities,
        )
        aio = await asyncio.start_server(self.server.handle_connection, "127.0.0.1", 0)
        self.port = await self.server.serve(aio, host="127.0.0.1")
        if pump:
            self._pump_stop = asyncio.Event()

            async def _pump() -> None:
                assert self.queue is not None and self._pump_stop is not None
                while not self._pump_stop.is_set():
                    self.queue.pump_one()
                    await asyncio.sleep(0.001)

            self._pump_task = asyncio.create_task(_pump())
        return self.port

    async def stop(self) -> None:
        if self._pump_stop is not None:
            self._pump_stop.set()
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.server is not None:
            await self.server.stop()
        await asyncio.sleep(0.02)


def _scene_query_request(request_id: str = "req_q", scene_epoch: int = 1) -> BridgeRequest:
    return BridgeRequest.from_dict(
        {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": request_id,
            "operation": "scene.query",
            "deadline_ms": 5000,
            "scene_epoch": scene_epoch,
            "payload": {"include_selection": False, "node_paths": [], "include_geometry_stats": False},
        }
    )


# ==========================================================================
# 1. server advertises sorted capabilities with changeset.v1
# ==========================================================================


@async_test
async def test_server_ack_advertises_sorted_capabilities(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello = {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token}
        writer.write((len(json.dumps(hello))).to_bytes(4, "big") + json.dumps(hello).encode())
        await writer.drain()
        header = await reader.readexactly(4)
        length = int.from_bytes(header, "big")
        ack = json.loads(await reader.readexactly(length))
        assert ack["ok"] is True
        assert ack["capabilities"] == ["changeset.v1"]  # sorted + unique
        writer.close()
    finally:
        await harness.stop()


@async_test
async def test_client_stores_capabilities_after_hello(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert CHANGESET_V1 in client.capabilities
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 2. legacy/new hello compatibility + malformed ack fail closed (client side)
# ==========================================================================


def _ack_bytes(caps: object | None, ok: bool = True) -> bytes:
    ack: dict[str, object] = {"protocol": PROTOCOL, "kind": "hello", "ok": ok}
    if caps is not None:
        ack["capabilities"] = caps
    text = json.dumps(ack, sort_keys=True, separators=(",", ":"))
    return (len(text)).to_bytes(4, "big") + text.encode()


class _FakeTransport:
    def __init__(self, inbox: bytes) -> None:
        self._inbox = bytearray(inbox)
        self.outbox = bytearray()

    async def read_exactly(self, n: int) -> bytes:
        while len(self._inbox) < n:
            raise asyncio.IncompleteReadError(bytes(self._inbox), n)
        chunk = bytes(self._inbox[:n])
        del self._inbox[:n]
        return chunk

    async def write(self, data: bytes) -> None:
        self.outbox.extend(data)

    async def close(self) -> None:
        pass


@async_test
async def test_legacy_ack_without_capabilities_accepted() -> None:
    # A legacy server ack (no capabilities key) is valid; caps read as empty.
    fake = _FakeTransport(inbox=_ack_bytes(caps=None))
    client = BridgeClient(
        host="127.0.0.1", port=1, identity=create_bridge_identity(), transport_factory=lambda: fake
    )
    await client.open()
    assert client.capabilities == ()


@pytest.mark.parametrize(
    "bad_caps",
    [
        "changeset.v1",  # not a list
        ["changeset.v1", "changeset.v1"],  # duplicate
        ["scene.v1", "changeset.v1"],  # unsorted
        ["changeset.v1", 7],  # non-string element
        ["CHANGES.V1"],  # bad grammar
    ],
    ids=["non-list", "duplicate", "unsorted", "non-string", "bad-grammar"],
)
def test_malformed_capability_ack_fails_closed(bad_caps: object) -> None:
    async def body() -> None:
        fake = _FakeTransport(inbox=_ack_bytes(caps=bad_caps))
        client = BridgeClient(
            host="127.0.0.1",
            port=1,
            identity=create_bridge_identity(),
            transport_factory=lambda: fake,
        )
        with pytest.raises(BridgeClientError):
            await client.open()

    asyncio.run(body())


@async_test
async def test_absent_capability_preflight_sends_no_frame(tmp_path: Path) -> None:
    # Legacy ack -> no changeset.v1 -> preflight raises capability_unavailable
    # and never writes a request frame to the transport.
    fake = _FakeTransport(inbox=_ack_bytes(caps=None))
    client = BridgeClient(
        host="127.0.0.1", port=1, identity=create_bridge_identity(), transport_factory=lambda: fake
    )
    await client.open()
    adapter = HoudiniSceneAdapter(FakeHou(nodes=_standard_nodes([]), spy=[]))
    request = _preflight_for(
        adapter,
        operations=(SetParm(op_id="op1", target=_noderef("n_child", "/obj/ws/geo1", "geo"), parm_name="tx", value=0, expected_old_value=0),),
        affected=(_noderef("n_child", "/obj/ws/geo1", "geo"),),
    )
    frames_before = len(fake.outbox)
    with pytest.raises(BridgeClientError) as exc:
        await client.preflight(request)
    assert exc.value.code == "bridge.capability_unavailable"
    # No frame was sent for the preflight request.
    assert len(fake.outbox) == frames_before


# ==========================================================================
# 3. successful preflight returns bounded typed facts
# ==========================================================================


@async_test
async def test_preflight_success_returns_facts(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(
                SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),
            ),
            affected=(target,),
        )
        result = await client.preflight(request)
        assert isinstance(result, PreflightResult)
        assert result.scene_may_have_changed is False
        assert result.workspace_id == WS
        assert result.workspace_revision is not None
        # one node fact for the affected node, exists, mirrored identity matches
        assert len(result.node_facts) == 1
        fact = result.node_facts[0]
        assert fact.exists is True
        assert fact.actual_path == "/obj/ws/geo1"
        assert fact.actual_type == "geo"
        assert fact.node_id == "n_child"
        assert fact.workspace_id == WS
        assert fact.is_locked is False
        # parm fact for tx
        assert len(result.parm_facts) == 1
        assert result.parm_facts[0].parm_name == "tx"
        assert result.parm_facts[0].exists is True
        assert result.parm_facts[0].value == 0
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_tuple_parm_fact(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(
                SetParm(op_id="op1", target=target, parm_name="t", value=(1.0, 2.0, 3.0), expected_old_value=(0.0, 1.0, 0.0)),
            ),
            affected=(target,),
            affected_paths=("/obj/ws/geo1",),
        )
        result = await client.preflight(request)
        assert result.parm_facts[0].parm_name == "t"
        assert result.parm_facts[0].value == (0.0, 1.0, 0.0)  # actual current value
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_wire_fact_present_and_null(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        src_ref = _noderef("n_src", "/obj/ws/src1", "xform")
        # input 0 is wired to src1; input 1 is unconnected (null).
        request = _preflight_for(
            harness.adapter,
            operations=(
                ConnectInput(
                    op_id="op1",
                    target=target,
                    input_index=0,
                    source=src_ref,
                    source_output_index=0,
                    expected_old_source=None,
                ),
            ),
            affected=(target, src_ref),
        )
        result = await client.preflight(request)
        by_idx = {f.input_index: f for f in result.wire_facts}
        assert by_idx[0].source is not None
        assert by_idx[0].source.source.path == "/obj/ws/src1"
        assert by_idx[0].source.source_output_index == 0
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_locked_node_reported(tmp_path: Path) -> None:
    spy: list = []
    nodes = _standard_nodes(spy)
    nodes["/obj/ws/geo1"].hard_locked = True
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        result = await client.preflight(request)
        assert result.node_facts[0].is_locked is True
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_performs_zero_writes(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
            preconditions=(ParmValueEquals(target=target, parm_name="tx", value=0),),
        )
        await client.preflight(request)
        await client.close()
    finally:
        await harness.stop()
    assert harness.spy == [], f"preflight performed writes: {harness.spy!r}"


# ==========================================================================
# 4. condition results + all_preconditions_hold derived from facts
# ==========================================================================


@async_test
async def test_preflight_conditions_evaluated_from_facts(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        binding = harness.adapter.binding()
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
            preconditions=(
                SceneBindingEquals(instance_id=binding.instance_id, scene_epoch=binding.scene_epoch),
                NodeIdentityEquals(node=target),
                ParmValueEquals(target=target, parm_name="tx", value=99),  # fails: actual is 0
            ),
        )
        result = await client.preflight(request)
        kinds = {r.kind for r in result.condition_results}
        assert "scene.binding_equals" in kinds
        assert "parm.value_equals" in kinds
        # the binding + identity preconditions hold, but tx==99 does not
        assert result.all_preconditions_hold is False
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_all_hold_when_conditions_match(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        binding = harness.adapter.binding()
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
            preconditions=(
                SceneBindingEquals(instance_id=binding.instance_id, scene_epoch=binding.scene_epoch),
                NodeIdentityEquals(node=target),
                ParmValueEquals(target=target, parm_name="tx", value=0),
            ),
        )
        result = await client.preflight(request)
        assert result.all_preconditions_hold is True
        assert all(r.passed for r in result.condition_results)
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 5. fail-closed: stale epoch / manifest / missing / mismatches / ambiguity
# ==========================================================================


@async_test
async def test_preflight_stale_epoch_rejected(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        binding = harness.adapter.binding()
        # Build a self-consistent request whose epoch is one ahead of the scene.
        stale_epoch = binding.scene_epoch + 1
        stale_binding = SceneBinding(
            instance_id=binding.instance_id,
            scene_epoch=stale_epoch,
            hip_path=None,
            observed_revision="x",
        )
        cs_stale = _changeset(
            stale_binding,
            (SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            (target,),
        )
        request = PreflightRequest.build(
            request_id="req_stale",
            deadline_ms=5000,
            scene_epoch=stale_epoch,
            changeset=cs_stale,
            workspace=_standard_manifest(binding.instance_id, stale_epoch),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "bridge.stale_scene"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_missing_node_rejected(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        missing = _noderef("n_gone", "/obj/ws/nope", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=missing, parm_name="tx", value=0, expected_old_value=0),),
            affected=(missing,),
            affected_paths=("/obj/ws/nope",),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        assert exc.value.retryable is True
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_type_mismatch_rejected(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        wrong_type = _noderef("n_child", "/obj/ws/geo1", "xform")  # actual is geo
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=wrong_type, parm_name="tx", value=0, expected_old_value=0),),
            affected=(wrong_type,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_node_id_mismatch_rejected(tmp_path: Path) -> None:
    # Scene node mirrors a different node_id than the request claims.
    spy: list = []
    nodes = _standard_nodes(spy)
    nodes["/obj/ws/geo1"].user_data = _mirror("n_OTHER")
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")  # claims n_child; scene has n_OTHER
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_workspace_mismatch_rejected(tmp_path: Path) -> None:
    # Scene node mirrors a workspace_id that disagrees with the manifest.
    spy: list = []
    nodes = _standard_nodes(spy)
    ud = dict(nodes["/obj/ws/geo1"].user_data)
    ud["eee.workspace_id"] = f"ws_{'9' * 32}"
    nodes["/obj/ws/geo1"].user_data = ud
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_parent_mismatch_rejected(tmp_path: Path) -> None:
    spy: list = []
    nodes = _standard_nodes(spy)
    # The manifest says geo1's parent is /obj/ws, but the scene moved it.
    nodes["/obj/ws/geo1"].parent_path = "/obj"
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_capability_role_mismatch_rejected(tmp_path: Path) -> None:
    spy: list = []
    nodes = _standard_nodes(spy)
    nodes["/obj/ws/geo1"].user_data = _mirror("n_child", role="supervisor")  # manifest says member
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_ambiguous_identity_rejected(tmp_path: Path) -> None:
    # Two scene nodes at different paths mirror the SAME stable node id. A
    # ChangeSet cannot list the same id twice in affected_nodes, so the second
    # reference arrives via a ConnectInput source — preflight still detects that
    # one stable id resolves to two distinct paths and fails closed.
    spy: list = []
    nodes = _standard_nodes(spy)
    dup = _FakeNode(
        "/obj/ws/geo2",
        type_name="geo",
        parent="/obj/ws",
        user_data=_mirror("n_child"),  # same id as /obj/ws/geo1
        spy=spy,
    )
    nodes["/obj/ws/geo2"] = dup
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        a = _noderef("n_child", "/obj/ws/geo1", "geo")
        b = _noderef("n_child", "/obj/ws/geo2", "geo")  # same node_id, different path
        request = _preflight_for(
            harness.adapter,
            operations=(
                ConnectInput(
                    op_id="op1",
                    target=a,
                    input_index=0,
                    source=b,
                    source_output_index=0,
                    expected_old_source=None,
                ),
            ),
            affected=(a,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "policy.ownership_ambiguous"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_unsupported_parm_value_rejected(tmp_path: Path) -> None:
    spy: list = []
    nodes = _standard_nodes(spy)
    # A parm whose value cannot be represented as a bounded scalar/tuple.
    nodes["/obj/ws/geo1"].parms = {"tx": _FakeParm(object())}
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.invalid"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_wrong_token_unauthorized(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        wrong = create_bridge_identity()
        client = BridgeClient(host="127.0.0.1", port=port, identity=wrong)
        with pytest.raises(BridgeClientError) as exc:
            await client.open()
        assert exc.value.code == "bridge.unauthorized"
    finally:
        await harness.stop()


# ==========================================================================
# 6. unknown operation / digest mismatch rejected at parse (fail closed)
# ==========================================================================


@async_test
async def test_preflight_unsupported_bridge_operation_rejected(tmp_path: Path) -> None:
    # changeset.apply is a known operation name but is NOT served in this slice;
    # strict typed dispatch must reject it before any HOM access.
    harness = _Harness()
    port = await harness.start(tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello = {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token}
        raw = json.dumps(hello, sort_keys=True, separators=(",", ":")).encode()
        writer.write(len(raw).to_bytes(4, "big") + raw)
        await writer.drain()
        # read + discard the ack frame
        ack_len = int.from_bytes(await reader.readexactly(4), "big")
        await reader.readexactly(ack_len)
        bad = {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": "r1",
            "operation": "changeset.apply",  # not served in Task 16-C
            "deadline_ms": 5000,
            "scene_epoch": 1,
            "payload": {},
        }
        bad_raw = json.dumps(bad, sort_keys=True, separators=(",", ":")).encode()
        writer.write(len(bad_raw).to_bytes(4, "big") + bad_raw)
        await writer.drain()
        resp_len = int.from_bytes(await reader.readexactly(4), "big")
        resp = json.loads(await reader.readexactly(resp_len))
        assert resp["ok"] is False
        assert resp["error"]["code"] == "bridge.invalid_request"
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 7. shared FIFO: ordering, deadline, cancellation, shutdown
# ==========================================================================


@async_test
async def test_preflight_and_scene_query_share_fifo_order(tmp_path: Path) -> None:
    # scene.query and changeset.preflight share ONE bounded FIFO: each is
    # processed one-at-a-time by the single main-thread pump, in submit order.
    harness = _Harness()
    port = await harness.start(tmp_path, pump=False)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None and harness.queue is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        preflight_req = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        # 1) scene.query queues; one pump consumes exactly it.
        qt = asyncio.create_task(client.request(_scene_query_request("req_q")))
        await asyncio.sleep(0.05)
        assert harness.queue.pending_count == 1
        harness.queue.pump_one()
        query_result = await qt
        assert isinstance(query_result, SceneQueryResult)
        assert harness.queue.pending_count == 0
        # 2) preflight queues on the SAME queue; one pump consumes exactly it.
        pt = asyncio.create_task(client.preflight(preflight_req))
        await asyncio.sleep(0.05)
        assert harness.queue.pending_count == 1
        harness.queue.pump_one()
        preflight_result = await pt
        assert isinstance(preflight_result, PreflightResult)
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_deadline_when_queue_starved(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path, pump=False)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        preflight_req = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
            request_id="req_deadline",
            deadline_ms=300,
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(preflight_req)
        assert exc.value.code == "bridge.deadline_exceeded"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_shutdown_resolves_pending(tmp_path: Path) -> None:
    harness = _Harness()
    port = await harness.start(tmp_path, pump=False)
    client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
    await client.open()
    assert harness.adapter is not None
    target = _noderef("n_child", "/obj/ws/geo1", "geo")
    preflight_req = _preflight_for(
        harness.adapter,
        operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
        affected=(target,),
        request_id="req_pending",
        deadline_ms=30000,
    )
    task = asyncio.create_task(client.preflight(preflight_req))
    await asyncio.sleep(0.05)  # queued, not pumped
    await harness.stop()  # shutdown drains the queue
    done, _pending = await asyncio.wait({task}, timeout=3.0)
    assert task in done
    with pytest.raises(BridgeClientError):
        task.result()


# ==========================================================================
# 8. regression: scene.query still works alongside preflight capability
# ==========================================================================


@async_test
async def test_scene_query_unchanged_with_capability_advertised(tmp_path: Path) -> None:
    spy: list = []
    nodes = _standard_nodes(spy)
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        result = await client.request(_scene_query_request("req_q"))
        assert isinstance(result, SceneQueryResult)
        await client.close()
    finally:
        await harness.stop()


# ==========================================================================
# 9. structural: preflight adapter + server module boundaries
# ==========================================================================


def test_preflight_adapter_module_imports_are_clean() -> None:
    import ast
    import inspect

    from houdini_side import changeset_executor as mod

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


def test_preflight_adapter_is_zero_write_by_construction() -> None:
    import inspect

    from houdini_side import changeset_executor as mod

    source = inspect.getsource(mod)
    # Mutation surfaces that must NEVER appear in the read-only preflight path.
    # (.eval( is intentionally excluded: hou.Parm.eval() is a bounded READ.)
    for forbidden in (
        ".createNode(",
        ".destroy(",
        ".setInput(",
        ".setUserData(",
        ".set(",
        "undos.",
        ".save(",
        ".loadHip",
        ".load(",
        ".clear(",
        "installFile(",
        "hda.",
        "subprocess",
        " exec(",
    ):
        assert forbidden not in source, f"forbidden write surface in executor: {forbidden!r}"


def test_preflight_adapter_no_houdini_read_in_main_thread_only() -> None:
    # The adapter performs all reads through a single synchronous preflight()
    # that the queue pumps on the main thread; no asyncio/threading import.
    import ast
    import inspect

    from houdini_side import changeset_executor as mod

    tree = ast.parse(inspect.getsource(mod))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    assert "asyncio" not in mods
    assert "threading" not in mods


# ==========================================================================
# 10. old server (no changeset.v1) fails closed for preflight
# ==========================================================================


@async_test
async def test_old_server_rejects_preflight_before_parsing(tmp_path: Path) -> None:
    # A server that does not advertise changeset.v1 must reject a preflight
    # request with bridge.capability_unavailable, before any HOM access.
    harness = _Harness()
    port = await harness.start(tmp_path, capabilities=())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        hello = {"protocol": PROTOCOL, "kind": "hello", "token": harness.identity.token}
        raw = json.dumps(hello, sort_keys=True, separators=(",", ":")).encode()
        writer.write(len(raw).to_bytes(4, "big") + raw)
        await writer.drain()
        ack_len = int.from_bytes(await reader.readexactly(4), "big")
        ack = json.loads(await reader.readexactly(ack_len))
        assert ack["ok"] is True
        # No changeset.v1 advertised (empty list, or legacy-omitted).
        assert CHANGESET_V1 not in ack.get("capabilities", [])
        # Send a changeset.preflight frame (payload need not parse; capability
        # is checked before parsing).
        req = {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": "r1",
            "operation": "changeset.preflight",
            "deadline_ms": 5000,
            "scene_epoch": 1,
            "payload": {},
        }
        req_raw = json.dumps(req, sort_keys=True, separators=(",", ":")).encode()
        writer.write(len(req_raw).to_bytes(4, "big") + req_raw)
        await writer.drain()
        resp_len = int.from_bytes(await reader.readexactly(4), "big")
        resp = json.loads(await reader.readexactly(resp_len))
        assert resp["ok"] is False
        assert resp["error"]["code"] == "bridge.capability_unavailable"
        # No writes occurred (the scene was never touched).
        assert harness.spy == []
        writer.close()
    finally:
        await harness.stop()


# ==========================================================================
# 11. identity integrity: stable-id resolution BEFORE path + manifest facts
#
# A path is a locator, never identity. An owned NodeRef (node_id != None) must
# resolve to the unique current node whose mirrored eee.node_id agrees, even
# when its current path differs from the requested path; the manifest facts
# (schema_version, created_by_run, path/type/parent/capability/role) then
# decide whether that resolution is still fresh. Same node_id mirrored at two
# scene paths is full identity ambiguity and fails closed even when only one of
# the paths is referenced by the ChangeSet.
# ==========================================================================


def _moved_child_nodes(spy: list) -> dict[str, _FakeNode]:
    """Standard workspace, but the child geo was renamed/moved to a new path.

    The mirrored stable node id (n_child) is unchanged; only the path differs
    from the stale path the request still carries.
    """
    root = _FakeNode("/obj/ws", type_name="subnet", parent="/obj", user_data=_mirror("n_root", role="root"), spy=spy)
    src = _FakeNode("/obj/ws/src1", type_name="xform", parent="/obj/ws", user_data=_mirror("n_src"), spy=spy)
    geo = _FakeNode(
        "/obj/ws/geo_moved",
        type_name="geo",
        parent="/obj/ws",
        user_data=_mirror("n_child"),
        parms={"tx": _FakeParm(0)},
        spy=spy,
    )
    return {"/obj/ws": root, "/obj/ws/src1": src, "/obj/ws/geo_moved": geo}


def _manifest_for(adapter: HoudiniSceneAdapter, owned_nodes: tuple[OwnedNodeRef, ...]) -> WorkspaceManifest:
    binding = adapter.binding()
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id=binding.instance_id,
        scene_epoch=binding.scene_epoch,
        roots=(owned_nodes[0],),
        nodes=owned_nodes,
        created_by_run=RUN,
        updated_at=NOW,
    )


def _request_with_manifest(
    adapter: HoudiniSceneAdapter,
    *,
    operations: tuple[object, ...],
    affected: tuple[NodeRef, ...],
    manifest: WorkspaceManifest,
    preconditions: tuple = (),
) -> PreflightRequest:
    binding = adapter.binding()
    cs = _changeset(binding, operations, affected, preconditions=preconditions)
    return PreflightRequest.build(
        request_id="req_identity",
        deadline_ms=5000,
        scene_epoch=binding.scene_epoch,
        changeset=cs,
        workspace=manifest,
    )


@async_test
async def test_preflight_owned_node_resolved_by_id_at_moved_path(tmp_path: Path) -> None:
    # The request carries a STALE path (/obj/ws/geo1), but the scene moved the
    # node to /obj/ws/geo_moved while keeping its mirrored stable id. The
    # resolver must follow the stable id FIRST, find the node at its current
    # path, and — because the manifest agrees with the current path — succeed.
    spy: list = []
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=_moved_child_nodes(spy))
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        root_o = _owned("n_root", "/obj/ws", "subnet", "/obj", role="root")
        src_o = _owned("n_src", "/obj/ws/src1", "xform", "/obj/ws")
        child_o = _owned("n_child", "/obj/ws/geo_moved", "geo", "/obj/ws")  # manifest matches the NEW path
        manifest = _manifest_for(harness.adapter, (root_o, src_o, child_o))
        stale_ref = _noderef("n_child", "/obj/ws/geo1", "geo")  # stale path, correct id
        request = _request_with_manifest(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=stale_ref, parm_name="tx", value=0, expected_old_value=0),),
            affected=(stale_ref,),
            manifest=manifest,
        )
        result = await client.preflight(request)
        assert isinstance(result, PreflightResult)
        assert len(result.node_facts) == 1
        # Resolved by stable id to the CURRENT path, not the stale requested path.
        assert result.node_facts[0].exists is True
        assert result.node_facts[0].actual_path == "/obj/ws/geo_moved"
        assert result.node_facts[0].node_id == "n_child"
        # The parm fact is read from the resolved (moved) node, not the stale path.
        assert len(result.parm_facts) == 1
        assert result.parm_facts[0].parm_name == "tx"
        assert result.parm_facts[0].exists is True
        await client.close()
    finally:
        await harness.stop()
    assert spy == [], f"identity resolution performed writes: {spy!r}"


@async_test
async def test_preflight_owned_node_stale_when_manifest_path_disagrees(tmp_path: Path) -> None:
    # Same moved node, but the manifest still records the OLD path. Stable-id
    # resolution finds the node at the new path; the manifest fact comparison
    # (path vs the CURRENT scene path, not the request path) then fails closed.
    spy: list = []
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=_moved_child_nodes(spy))
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        root_o = _owned("n_root", "/obj/ws", "subnet", "/obj", role="root")
        src_o = _owned("n_src", "/obj/ws/src1", "xform", "/obj/ws")
        child_o = _owned("n_child", "/obj/ws/geo1", "geo", "/obj/ws")  # manifest still has the STALE path
        manifest = _manifest_for(harness.adapter, (root_o, src_o, child_o))
        stale_ref = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _request_with_manifest(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=stale_ref, parm_name="tx", value=0, expected_old_value=0),),
            affected=(stale_ref,),
            manifest=manifest,
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_ambiguous_identity_with_unreferenced_duplicate(tmp_path: Path) -> None:
    # The SAME stable node id is mirrored at two scene paths, but the ChangeSet
    # references only ONE of them. Path-based resolution would not see the
    # second path; stable-id resolution must still detect full identity
    # ambiguity and fail closed.
    spy: list = []
    nodes = _standard_nodes(spy)
    dup = _FakeNode(
        "/obj/ws/geo2",
        type_name="geo",
        parent="/obj/ws",
        user_data=_mirror("n_child"),  # same id as /obj/ws/geo1
        spy=spy,
    )
    nodes["/obj/ws/geo2"] = dup
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        only = _noderef("n_child", "/obj/ws/geo1", "geo")  # geo2 is NOT referenced
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=only, parm_name="tx", value=0, expected_old_value=0),),
            affected=(only,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "policy.ownership_ambiguous"
        await client.close()
    finally:
        await harness.stop()


async def _expect_stale_after_mirror_mutation(
    tmp_path: Path, mutate: Callable[[dict[str, str]], None]
) -> None:
    """Run a standard workspace preflight after mutating geo1's mirror; expect stale."""
    spy: list = []
    nodes = _standard_nodes(spy)
    ud = dict(nodes["/obj/ws/geo1"].user_data)
    mutate(ud)
    nodes["/obj/ws/geo1"].user_data = ud
    harness = _Harness()
    port = await harness.start(tmp_path, nodes=nodes)
    try:
        client = BridgeClient(host="127.0.0.1", port=port, identity=harness.identity)
        await client.open()
        assert harness.adapter is not None
        target = _noderef("n_child", "/obj/ws/geo1", "geo")
        request = _preflight_for(
            harness.adapter,
            operations=(SetParm(op_id="op1", target=target, parm_name="tx", value=0, expected_old_value=0),),
            affected=(target,),
        )
        with pytest.raises(BridgeClientError) as exc:
            await client.preflight(request)
        assert exc.value.code == "changeset.stale"
        await client.close()
    finally:
        await harness.stop()


@async_test
async def test_preflight_missing_schema_version_rejected(tmp_path: Path) -> None:
    def mutate(ud: dict[str, str]) -> None:
        ud.pop("eee.schema_version", None)

    await _expect_stale_after_mirror_mutation(tmp_path, mutate)


@async_test
async def test_preflight_wrong_schema_version_rejected(tmp_path: Path) -> None:
    def mutate(ud: dict[str, str]) -> None:
        ud["eee.schema_version"] = "2"  # not the supported schema-v1 value

    await _expect_stale_after_mirror_mutation(tmp_path, mutate)


@async_test
async def test_preflight_missing_created_by_run_rejected(tmp_path: Path) -> None:
    def mutate(ud: dict[str, str]) -> None:
        ud.pop("eee.created_by_run", None)

    await _expect_stale_after_mirror_mutation(tmp_path, mutate)


@async_test
async def test_preflight_wrong_created_by_run_rejected(tmp_path: Path) -> None:
    def mutate(ud: dict[str, str]) -> None:
        ud["eee.created_by_run"] = f"run_{'3' * 32}"  # disagrees with the manifest

    await _expect_stale_after_mirror_mutation(tmp_path, mutate)
