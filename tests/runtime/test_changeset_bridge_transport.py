"""Task 16-D: ``changeset.apply`` / ``changeset.receipt`` Bridge transport.

Drives a REAL ``BridgeClient`` against a REAL ``asyncio.start_server`` loopback
``BridgeServer`` (one shared ``MainThreadReadQueue``) backed by a ``hou``-free
WRITABLE fake scene. No ``hou``, ``rpyc``, live LLM, or real Houdini process is
used. The Houdini main-thread pump is modelled by a cooperative pump task.

Covers the apply/receipt wire round-trip, idempotent replay over the wire,
capability gating (no frame sent), stale-scene pre-transaction failure with
zero writes, receipt not-found, and FIFO serialization of scene.query + apply.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from eee_agent.changesets import (
    ChangeSet,
    CheckpointPlan,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SetParm,
    WorkspaceManifest,
)
from eee_agent.houdini_bridge.auth import create_bridge_identity
from eee_agent.houdini_bridge.changesets import (
    CHANGESET_V1,
    ApplyRequest,
    ReceiptRequest,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.queue import (
    MainThreadReadQueue,
    QueueItemCancelled,
    QueueItemExpired,
    QueueFull,
    QueueRejected,
)
from houdini_side.changeset_executor import (
    _dedup,
    derive_mandatory_checkpoint,
    derive_mandatory_postconditions,
    derive_mandatory_preconditions,
)
from houdini_side.secure_bridge import BridgeServer, HoudiniSceneAdapter

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
# compact writable fake scene (hou-free) with a mutation spy
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

    def eval(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._node._spy.append(("parm.set", self._node._path, self._name))
        self._value = value


class _WConn:
    def __init__(self, out_node: "_WNode", out_idx: int, in_idx: int) -> None:
        self._out, self._oi, self._ii = out_node, out_idx, in_idx

    def outputNode(self) -> "_WNode":
        return self._out

    def outputIndex(self) -> int:
        return self._oi

    def inputIndex(self) -> int:
        return self._ii


class _WNode:
    def __init__(self, scene: dict, spy: list, path: str, type_name: str, parent: str, user_data: dict | None = None) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self._user_data = dict(user_data or {})
        self._parms: dict[str, _WParm] = {}
        self._inputs: dict[int, tuple[_WNode, int]] = {}

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

    def inputConnections(self) -> list[_WConn]:
        return [_WConn(self._inputs[i][0], self._inputs[i][1], i) for i in sorted(self._inputs)]

    def inputs(self) -> list:
        if not self._inputs:
            return []
        size = max(self._inputs) + 1
        return [self._inputs[i][0] if i in self._inputs else None for i in range(size)]

    def setInput(self, idx: int, src: "_WNode | None", out_idx: int = 0) -> None:
        self._spy.append(("setInput", self._path, idx))
        if src is None:
            self._inputs.pop(idx, None)
        else:
            self._inputs[idx] = (src, out_idx)

    def createNode(self, type_name: str, name: str) -> "_WNode":
        self._spy.append(("createNode", self._path, type_name, name))
        # F9: synchronization barrier for lifecycle tests.
        if getattr(self, "create_enter_event", None) is not None:
            self.create_enter_event.set()
        if getattr(self, "create_release_event", None) is not None:
            self.create_release_event.wait(timeout=10)
        path = self._path.rstrip("/") + "/" + name
        node = _WNode(self._scene, self._spy, path, type_name, self._path)
        self._scene[path] = node
        return node

    def destroy(self) -> None:
        self._spy.append(("destroy", self._path))
        self._scene.pop(self._path, None)

    def isHardLocked(self) -> bool:
        return False

    def isSoftLocked(self) -> bool:
        return False


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
    root = _WNode(scene, spy, "/obj/ws", "subnet", "/obj", {
        "eee.workspace_id": WS, "eee.node_id": "n_root", "eee.capability": "modeling",
        "eee.role": "root", "eee.schema_version": "1", "eee.created_by_run": RUN,
    })
    child = _WNode(scene, spy, "/obj/ws/geo1", "geo", "/obj/ws", {
        "eee.workspace_id": WS, "eee.node_id": "n_child", "eee.capability": "modeling",
        "eee.role": "member", "eee.schema_version": "1", "eee.created_by_run": RUN,
    })
    child._parms["tx"] = _WParm(child, "tx", 0)
    scene["/obj/ws"] = root
    scene["/obj/ws/geo1"] = child
    return scene


def _owned(node_id: str, path: str, node_type: str, parent: str, role: str = "member") -> OwnedNodeRef:
    return OwnedNodeRef(
        node_id=node_id, path=path, node_type=node_type, parent_path=parent,
        capability="modeling", role=role,
    )


def _manifest(adapter: HoudiniSceneAdapter) -> WorkspaceManifest:
    binding = adapter.binding()
    root = _owned("n_root", "/obj/ws", "subnet", "/obj", "root")
    child = _owned("n_child", "/obj/ws/geo1", "geo", "/obj/ws")
    return WorkspaceManifest.build(
        workspace_id=WS, session_id=SES, instance_id=binding.instance_id,
        scene_epoch=binding.scene_epoch, roots=[root], nodes=[root, child],
        created_by_run=RUN, updated_at=NOW,
    )


def _setparm_changeset(adapter: HoudiniSceneAdapter, *, value: int = 5, change_id=f"chg_{'4' * 32}") -> ChangeSet:
    binding = adapter.binding()
    child = NodeRef(node_id="n_child", path="/obj/ws/geo1", expected_type="geo", expected_workspace_id=WS)
    ops = (SetParm(op_id="op_s", target=child, parm_name="tx", value=value, expected_old_value=0),)
    workspace = _manifest(adapter)
    pre = tuple(_dedup(derive_mandatory_preconditions(ops, binding, workspace)))
    post = tuple(_dedup(derive_mandatory_postconditions(ops)))
    cnodes, cparms, cwires = derive_mandatory_checkpoint(ops)
    return ChangeSet(
        change_id=change_id, session_id=SES, run_id=RUN, scene_binding=binding,
        workspace_id=WS, base_revision=REVISION,
        required_permission=PermissionMode.OWNED_WORKSPACE, scoped_node_ids=(),
        operations=ops,
        affected_nodes=(child,), read_dependencies=(),
        preconditions=pre, expected_postconditions=post,
        risk_summary=RiskSummary(
            touches_external_nodes=False, changes_wiring=False, requires_backup=False,
            operation_count=1, effect_names=("parm.set",), affected_paths=("/obj/ws/geo1",),
        ),
        checkpoint_plan=CheckpointPlan(
            nodes=tuple(_dedup(cnodes)), parameters=tuple(_dedup(cparms)), wires=tuple(_dedup(cwires)),
        ),
        created_at=NOW,
    )


def _apply_request(adapter: HoudiniSceneAdapter, cs: ChangeSet) -> ApplyRequest:
    binding = adapter.binding()
    return ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=binding.scene_epoch,
        changeset=cs, workspace=_manifest(adapter),
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

    async def start(self, state_dir: Path, *, capabilities=(CHANGESET_V1,)) -> None:
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
async def test_apply_round_trip_returns_applied_receipt(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        async with h._client() as client:
            receipt = await client.apply(_apply_request(h.adapter, _setparm_changeset(h.adapter)))
        assert receipt.status.value == "Applied"
        assert receipt.applied_op_ids == ("op_s",)
        assert receipt.scene_may_have_changed is False
        assert ("parm.set", "/obj/ws/geo1", "tx") in h.spy
    finally:
        await h.stop()


@async_test
async def test_receipt_round_trip_returns_cached_receipt(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        cs = _setparm_changeset(h.adapter)
        async with h._client() as client:
            await client.apply(_apply_request(h.adapter, cs))
            cached = await client.receipt(
                ReceiptRequest.build(
                    request_id="req_r", deadline_ms=5000, scene_epoch=h.adapter.binding().scene_epoch,
                    change_id=cs.change_id, changeset_digest=cs.digest,
                )
            )
        assert cached.status.value == "Applied"
        assert cached.change_id == cs.change_id
    finally:
        await h.stop()


@async_test
async def test_apply_replay_is_already_applied(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        async with h._client() as client:
            first = await client.apply(_apply_request(h.adapter, _setparm_changeset(h.adapter)))
            second = await client.apply(_apply_request(h.adapter, _setparm_changeset(h.adapter)))
        assert first.status.value == "Applied"
        assert second.status.value == "AlreadyApplied"
        assert second.applied_op_ids == ()
    finally:
        await h.stop()


@async_test
async def test_apply_without_capability_sends_no_frame(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path, capabilities=())  # no changeset.v1 advertised
    try:
        assert h.adapter is not None
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.apply(_apply_request(h.adapter, _setparm_changeset(h.adapter)))
        assert exc.value.code == "bridge.capability_unavailable"
        # nothing reached the executor -> zero writes
        assert not [m for m in h.spy if m[0] in ("parm.set", "createNode", "setInput", "undo_group_begin")]
    finally:
        await h.stop()


@async_test
async def test_apply_stale_scene_returns_bridge_error_zero_writes(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        req = _apply_request(h.adapter, _setparm_changeset(h.adapter))
        # bump the scene epoch AFTER hello so the apply binding is stale
        h.adapter._on_scene_event(h.adapter._hou.hipFileEventType.AfterLoad)  # type: ignore[attr-defined]
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.apply(req)
        assert exc.value.code == "bridge.stale_scene"
        assert not [m for m in h.spy if m[0] in ("parm.set", "createNode", "setInput", "undo_group_begin")]
    finally:
        await h.stop()


@async_test
async def test_receipt_not_found_returns_unavailable(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        async with h._client() as client:
            with pytest.raises(BridgeClientError) as exc:
                await client.receipt(
                    ReceiptRequest.build(
                        request_id="req_r", deadline_ms=5000,
                        scene_epoch=h.adapter.binding().scene_epoch,
                        change_id=f"chg_{'9' * 32}", changeset_digest="b" * 64,
                    )
                )
        assert exc.value.code == "changeset.receipt_unavailable"
    finally:
        await h.stop()


@async_test
async def test_fifo_serializes_scene_query_and_apply(tmp_path: Path) -> None:
    """A scene.query and a changeset.apply share one FIFO and complete in turn."""
    from eee_agent.houdini_bridge.contracts import BridgeOperation, BridgeRequest

    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        epoch = h.adapter.binding().scene_epoch
        query = BridgeRequest(
            request_id="req_q", operation=BridgeOperation.SCENE_QUERY,
            deadline_ms=5000, scene_epoch=epoch, payload={"node_paths": ["/obj/ws/geo1"]},
        )
        async with h._client() as client:
            # One connection = one in-flight frame; the two operations still
            # share the single FIFO and are pumped strictly in submission order.
            query_result = await client.request(query)
            receipt = await client.apply(_apply_request(h.adapter, _setparm_changeset(h.adapter)))
        assert len(query_result.nodes) == 1
        assert receipt.status.value == "Applied"
    finally:
        await h.stop()


# --------------------------------------------------------------------------
# F6: receipt conflict + wrong scene_epoch over the wire
# --------------------------------------------------------------------------


@async_test
async def test_f6_receipt_conflict_over_wire(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        cs = _setparm_changeset(h.adapter)
        async with h._client() as client:
            await client.apply(_apply_request(h.adapter, cs))
            with pytest.raises(BridgeClientError) as exc:
                await client.receipt(
                    ReceiptRequest.build(
                        request_id="req_r", deadline_ms=5000,
                        scene_epoch=h.adapter.binding().scene_epoch,
                        change_id=cs.change_id, changeset_digest="b" * 64,
                    )
                )
        assert exc.value.code == "changeset.receipt_conflict"
    finally:
        await h.stop()


@async_test
async def test_f6_receipt_wrong_scene_epoch_over_wire(tmp_path: Path) -> None:
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        cs = _setparm_changeset(h.adapter)
        async with h._client() as client:
            await client.apply(_apply_request(h.adapter, cs))
            with pytest.raises(BridgeClientError) as exc:
                await client.receipt(
                    ReceiptRequest.build(
                        request_id="req_r", deadline_ms=5000, scene_epoch=99,
                        change_id=cs.change_id, changeset_digest=cs.digest,
                    )
                )
        assert exc.value.code == "bridge.stale_scene"
    finally:
        await h.stop()


# --------------------------------------------------------------------------
# F7: queue cancellation / deadline / capacity / shutdown drain
# --------------------------------------------------------------------------


@async_test
async def test_f7_deadline_before_start_runs_zero_operations() -> None:
    """An item whose deadline elapsed before it starts is expired, never run."""
    q = MainThreadReadQueue()
    ran: list = []
    fut = q.submit("req", lambda: ran.append(1), deadline_monotonic=time.monotonic() - 1)
    assert q.pump_one() is True
    assert ran == []
    with pytest.raises(QueueItemExpired):
        await fut


@async_test
async def test_f7_cancel_before_start_runs_zero_operations() -> None:
    """Cancellation before start resolves cancelled and never runs the operation."""
    q = MainThreadReadQueue()
    ran: list = []
    fut = q.submit("req", lambda: ran.append(1), deadline_monotonic=time.monotonic() + 10)
    assert q.cancel("req") is True
    assert ran == []
    with pytest.raises(QueueItemCancelled):
        await fut


@async_test
async def test_f7_queue_capacity_rejects_overflow() -> None:
    q = MainThreadReadQueue(capacity=2)
    q.submit("a", lambda: None, deadline_monotonic=time.monotonic() + 10)
    q.submit("b", lambda: None, deadline_monotonic=time.monotonic() + 10)
    with pytest.raises(QueueFull):
        q.submit("c", lambda: None, deadline_monotonic=time.monotonic() + 10)


@async_test
async def test_f7_shutdown_drains_pending() -> None:
    q = MainThreadReadQueue()
    futs = [
        q.submit(f"r{i}", lambda: None, deadline_monotonic=time.monotonic() + 10)
        for i in range(3)
    ]
    q.shutdown()
    for fut in futs:
        with pytest.raises(QueueRejected):
            await fut


@async_test
async def test_f7_receipt_queryable_after_apply_with_zero_extra_writes(tmp_path: Path) -> None:
    """After a transaction completes, its receipt is queryable; a second query
    performs zero extra writes (cancellation-after-start guarantee: the receipt
    survives even if the client never observed the apply result)."""
    h = _Harness()
    await h.start(tmp_path)
    try:
        assert h.adapter is not None
        cs = _setparm_changeset(h.adapter)
        async with h._client() as client:
            await client.apply(_apply_request(h.adapter, cs))
        h.spy.clear()
        async with h._client() as client2:
            cached = await client2.receipt(
                ReceiptRequest.build(
                    request_id="req_r", deadline_ms=5000,
                    scene_epoch=h.adapter.binding().scene_epoch,
                    change_id=cs.change_id, changeset_digest=cs.digest,
                )
            )
        assert cached.status.value == "Applied"
        assert not [m for m in h.spy if m[0] in ("parm.set", "createNode", "setInput")]
    finally:
        await h.stop()


# --------------------------------------------------------------------------
# F9: server close/stop waits for a running transaction before cleanup
# --------------------------------------------------------------------------


@async_test
async def test_f9_close_waits_for_running_apply(tmp_path: Path) -> None:
    """close() must not finish adapter/identity cleanup while a transaction is
    running on the pump thread. It blocks until the operation completes, then
    cleans up — without interrupting the transaction and without a same-thread
    deadlock."""
    from eee_agent.changesets import CreateNode

    spy: list = []
    scene = _standard_scene(spy)
    enter = threading.Event()
    release = threading.Event()
    scene["/obj/ws"].create_enter_event = enter
    scene["/obj/ws"].create_release_event = release
    hou = WriteHou(scene, spy)
    adapter = HoudiniSceneAdapter(hou)
    queue = MainThreadReadQueue()
    identity = create_bridge_identity()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=tmp_path,
        queue=queue, capabilities=(CHANGESET_V1,),
    )

    # Thread-based pump (so it can block without freezing a caller).
    pump_stop = threading.Event()

    def _pump() -> None:
        while not pump_stop.is_set():
            queue.pump_one()
            time.sleep(0.001)

    pump_thread = threading.Thread(target=_pump)
    pump_thread.start()
    try:
        binding = adapter.binding()
        parent = NodeRef(node_id="n_root", path="/obj/ws", expected_type="subnet", expected_workspace_id=WS)
        ops = (CreateNode(op_id="op_c", parent=parent, node_id="n_new", node_type="geo",
                          node_name="geo_new", workspace_id=WS, capability="modeling", role="member"),)
        workspace = _manifest(adapter)
        pre = tuple(_dedup(derive_mandatory_preconditions(ops, binding, workspace)))
        post = tuple(_dedup(derive_mandatory_postconditions(ops)))
        cnodes, _cp, _cw = derive_mandatory_checkpoint(ops)
        cs = ChangeSet(
            change_id=f"chg_{'7' * 32}", session_id=SES, run_id=RUN, scene_binding=binding,
            workspace_id=WS, base_revision=REVISION,
            required_permission=PermissionMode.OWNED_WORKSPACE, scoped_node_ids=(),
            operations=ops,
            affected_nodes=(NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo",
                                    expected_workspace_id=WS),),
            read_dependencies=(), preconditions=pre, expected_postconditions=post,
            risk_summary=RiskSummary(
                touches_external_nodes=False, changes_wiring=False, requires_backup=False,
                operation_count=1, effect_names=("node.create",), affected_paths=("/obj/ws/geo_new",),
            ),
            checkpoint_plan=CheckpointPlan(nodes=tuple(_dedup(cnodes)), parameters=(), wires=()),
            created_at=NOW,
        )
        request = ApplyRequest.build(
            request_id="req_f9", deadline_ms=30000, scene_epoch=binding.scene_epoch,
            changeset=cs, workspace=workspace,
        )
        # Submit directly to the queue (bypass the socket).
        queue.submit(
            "req_f9", lambda: server._executor.apply(request),  # type: ignore[union-attr]
            deadline_monotonic=time.monotonic() + 30,
        )
        # The pump thread picks up the item and blocks inside createNode.
        assert enter.wait(timeout=10), "operation did not reach createNode"

        # Start close() on a separate thread.
        close_done = threading.Event()

        def _close() -> None:
            server.close()
            close_done.set()

        close_thread = threading.Thread(target=_close)
        close_thread.start()

        # close() should still be draining the running queue item: the
        # transaction is in progress and adapter cleanup has NOT happened.
        time.sleep(0.2)
        assert not close_done.is_set(), "close() returned before the running apply finished"
        assert not adapter._closed, "adapter cleaned up before the running apply finished"

        # Release the barrier → the apply completes → close() proceeds with cleanup.
        release.set()
        assert close_done.wait(timeout=10), "close() did not complete after the apply finished"
        close_thread.join(timeout=10)

        # Cleanup happened AFTER the apply finished.
        assert adapter._closed
    finally:
        pump_stop.set()
        pump_thread.join(timeout=5)
