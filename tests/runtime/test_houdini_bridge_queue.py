"""Task 15-C: bounded main-thread read queue + read-only Houdini scene adapter.

Part A (queue) is fully offline and ``hou``-independent. Part B (adapter) uses a
``fake_hou`` for dependency-injected offline tests — no Houdini process, no
``rpyc``, no socket. Async scenarios run via ``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import threading
import time
from typing import Awaitable, Callable

import pytest

from eee_agent.houdini_bridge.contracts import (
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)
from eee_agent.houdini_bridge.queue import (
    MainThreadReadQueue,
    QueueFull,
    QueueItem,
    QueueItemCancelled,
    QueueItemExpired,
    QueueItemState,
    QueueRejected,
)
from houdini_side.secure_bridge import (
    HoudiniAdapterError,
    HoudiniSceneAdapter,
    create_houdini_scene_adapter,
)


def async_test(coro: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    @functools.wraps(coro)
    def wrapper() -> None:
        asyncio.run(coro())

    return wrapper


def _module_imports(module: object, *, top_level_only: bool = False) -> set[str]:
    """Return the set of imported module names (AST, not docstring text).

    ``top_level_only`` inspects only module-body statements, so a lazily nested
    ``import hou`` inside a factory function is excluded.
    """
    import ast

    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    mods: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


# ==========================================================================
# Part A — main-thread read queue
# ==========================================================================


def _future_deadline(seconds: float = 10.0) -> float:
    return time.monotonic() + seconds


# --- 1. capacity validation ----------------------------------------------


@pytest.mark.parametrize("capacity", [0, -1, True, 1.5, "8"], ids=["zero", "neg", "bool", "float", "str"])
def test_capacity_must_be_positive_int(capacity: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        MainThreadReadQueue(capacity=capacity)  # type: ignore[arg-type]


# --- 2. request_id validation --------------------------------------------


@async_test
async def test_request_id_must_be_non_empty_string() -> None:
    queue = MainThreadReadQueue()
    with pytest.raises((TypeError, ValueError)):
        queue.submit("", lambda: None, deadline_monotonic=_future_deadline())
    with pytest.raises((TypeError, ValueError)):
        queue.submit(None, lambda: None, deadline_monotonic=_future_deadline())  # type: ignore[arg-type]


# --- 3. FIFO -------------------------------------------------------------


@async_test
async def test_fifo_order() -> None:
    queue = MainThreadReadQueue(capacity=4)
    order: list[str] = []
    f1 = queue.submit("r1", lambda: order.append("r1") or "r1", deadline_monotonic=_future_deadline())
    f2 = queue.submit("r2", lambda: order.append("r2") or "r2", deadline_monotonic=_future_deadline())
    f3 = queue.submit("r3", lambda: order.append("r3") or "r3", deadline_monotonic=_future_deadline())
    assert queue.pump_one() is True
    assert queue.pump_one() is True
    assert queue.pump_one() is True
    assert order == ["r1", "r2", "r3"]
    assert await f1 == "r1"
    assert await f2 == "r2"
    assert await f3 == "r3"


# --- 4. one pump_one runs exactly one operation --------------------------


@async_test
async def test_pump_one_runs_exactly_one_operation() -> None:
    queue = MainThreadReadQueue()
    counter = {"n": 0}

    def op() -> int:
        counter["n"] += 1
        return counter["n"]

    queue.submit("r1", op, deadline_monotonic=_future_deadline())
    queue.submit("r2", op, deadline_monotonic=_future_deadline())
    queue.submit("r3", op, deadline_monotonic=_future_deadline())
    assert queue.pump_one() is True
    assert counter["n"] == 1
    assert queue.pump_one() is True
    assert counter["n"] == 2
    assert counter["n"] == 2  # unchanged without another pump


# --- 5. pending_count ----------------------------------------------------


@async_test
async def test_pending_count() -> None:
    queue = MainThreadReadQueue()
    assert queue.pending_count == 0
    queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    queue.submit("r2", lambda: 2, deadline_monotonic=_future_deadline())
    assert queue.pending_count == 2
    queue.pump_one()
    assert queue.pending_count == 1
    queue.pump_one()
    assert queue.pending_count == 0


# --- 6. capacity full ----------------------------------------------------


@async_test
async def test_capacity_full_rejected() -> None:
    queue = MainThreadReadQueue(capacity=2)
    queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    queue.submit("r2", lambda: 2, deadline_monotonic=_future_deadline())
    with pytest.raises(QueueFull):
        queue.submit("r3", lambda: 3, deadline_monotonic=_future_deadline())


# --- 7. queued deadline expiry -------------------------------------------


@async_test
async def test_queued_deadline_expiry_skips_operation() -> None:
    queue = MainThreadReadQueue()
    called = {"n": 0}
    past = time.monotonic() - 1.0
    fut = queue.submit("r1", lambda: called.__setitem__("n", called["n"] + 1), deadline_monotonic=past)
    # pump with a `now` clearly after the deadline.
    assert queue.pump_one(now=past + 5.0) is True
    assert called["n"] == 0  # operation never ran
    with pytest.raises(QueueItemExpired):
        await fut


# --- 8. queued cancellation ---------------------------------------------


@async_test
async def test_queued_cancellation_skips_operation() -> None:
    queue = MainThreadReadQueue()
    called = {"n": 0}
    fut = queue.submit("r1", lambda: called.__setitem__("n", called["n"] + 1), deadline_monotonic=_future_deadline())
    assert queue.cancel("r1") is True
    assert queue.pump_one() is False  # nothing left to run
    assert called["n"] == 0
    with pytest.raises(QueueItemCancelled):
        await fut


# --- 9 + 10. running cancellation: no interrupt, discard result ----------


@async_test
async def test_running_cancellation_does_not_interrupt_and_discards_result() -> None:
    queue = MainThreadReadQueue()
    trace: list[str] = []

    def op() -> str:
        trace.append("started")
        assert queue.cancel("r1") is True  # cancel self while running
        trace.append("finished")
        return "SECRET_RESULT"

    fut = queue.submit("r1", op, deadline_monotonic=_future_deadline())
    assert queue.pump_one() is True
    # operation ran to completion (not interrupted)
    assert trace == ["started", "finished"]
    # but the result was discarded because it was cancelled mid-run
    with pytest.raises(QueueItemCancelled):
        await fut


# --- 11. operation exception propagates ----------------------------------


@async_test
async def test_operation_exception_propagates() -> None:
    queue = MainThreadReadQueue()

    def op() -> None:
        raise ValueError("houdini boom")

    fut = queue.submit("r1", op, deadline_monotonic=_future_deadline())
    queue.pump_one()
    with pytest.raises(ValueError, match="houdini boom"):
        await fut


# --- 12. pump_one with no work ------------------------------------------


def test_pump_one_no_work_returns_false() -> None:
    queue = MainThreadReadQueue()
    assert queue.pump_one() is False


# --- 13/14/15. shutdown --------------------------------------------------


@async_test
async def test_shutdown_rejects_new_submissions() -> None:
    queue = MainThreadReadQueue()
    queue.shutdown()
    with pytest.raises(QueueRejected):
        queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())


@async_test
async def test_shutdown_drains_queued_items() -> None:
    queue = MainThreadReadQueue()
    f1 = queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    f2 = queue.submit("r2", lambda: 2, deadline_monotonic=_future_deadline())
    assert queue.pending_count == 2
    queue.shutdown()
    assert queue.pending_count == 0
    with pytest.raises(QueueRejected):
        await f1
    with pytest.raises(QueueRejected):
        await f2


def test_shutdown_is_idempotent() -> None:
    queue = MainThreadReadQueue()
    queue.shutdown()
    queue.shutdown()  # must not raise
    queue.shutdown()


# --- 16/17. no background task / thread ----------------------------------


@async_test
async def test_no_background_task_created() -> None:
    queue = MainThreadReadQueue()
    before = set(asyncio.all_tasks())
    fut = queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    queue.pump_one()
    await fut
    queue.shutdown()
    after = set(asyncio.all_tasks())
    assert after == before


@async_test
async def test_no_background_thread_created() -> None:
    before = threading.active_count()
    queue = MainThreadReadQueue()
    fut = queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    queue.pump_one()
    await fut
    queue.shutdown()
    after = threading.active_count()
    assert after == before
    names = [t.name for t in threading.enumerate()]
    assert not any("queue" in n.lower() for n in names)


# --- 18/19. source hygiene -----------------------------------------------


def test_queue_module_imports_are_clean() -> None:
    from eee_agent.houdini_bridge import queue as queue_mod

    mods = _module_imports(queue_mod)
    assert "hou" not in mods
    assert "rpyc" not in mods
    # No Runtime / SQLite / nested-transaction coupling; no eee_agent at all.
    assert not any(m == "sqlite3" or "sqlite" in m.lower() for m in mods)
    assert not any(m.startswith("eee_agent") for m in mods)


# --- 20. duplicate request_id -------------------------------------------


@async_test
async def test_duplicate_request_id_rejected_while_pending() -> None:
    queue = MainThreadReadQueue()
    queue.submit("r1", lambda: 1, deadline_monotonic=_future_deadline())
    with pytest.raises(QueueRejected):
        queue.submit("r1", lambda: 2, deadline_monotonic=_future_deadline())


def test_queue_item_state_enum_members() -> None:
    # Sanity: the documented state machine is present.
    for name in (
        "QUEUED",
        "RUNNING",
        "COMPLETED",
        "CANCELLED",
        "EXPIRED",
        "FAILED",
        "REJECTED",
    ):
        assert hasattr(QueueItemState, name)
    assert QueueItemState.QUEUED == "queued"


def test_queue_item_is_frozen_snapshot() -> None:
    item = QueueItem(request_id="r1", deadline_monotonic=1.0)
    assert item.request_id == "r1"
    assert item.deadline_monotonic == 1.0
    with pytest.raises(Exception):
        item.request_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cross-thread safety: pump_one/cancel/shutdown run on the Houdini main thread
# while the awaited Future belongs to the transport thread's event loop. Future
# resolution MUST go through the owning loop's call_soon_threadsafe, otherwise
# the transport await never wakes. Each test uses a bounded watchdog so a
# regression cannot hang pytest.
# ---------------------------------------------------------------------------


def _spawn_transport(runner: Callable[[], Awaitable[object]]) -> tuple:
    """Run ``runner()`` in a daemon thread with its own event loop.

    Returns ``(thread, state)`` where ``state`` captures ``result``/``error``.
    The thread is a daemon so a stuck run cannot block process exit.
    """
    state: dict = {"result": None, "error": None}

    def target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            state["result"] = loop.run_until_complete(runner())
        except BaseException as exc:  # noqa: BLE001 — captured for assertions
            state["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=target, name="eee-transport-test", daemon=True)
    thread.start()
    return thread, state


_WATCHDOG = 5.0


@async_test
async def test_cross_thread_pump_one_wakes_transport_loop() -> None:
    # Runs in the test's own loop only to satisfy async_test; the real work
    # happens on the spawned transport thread + the calling (main) thread.
    queue = MainThreadReadQueue()
    submitted = threading.Event()

    async def runner() -> str:
        fut = queue.submit("r1", lambda: "ok", deadline_monotonic=_future_deadline())
        submitted.set()
        return await fut  # type: ignore[no-any-return]

    thread, state = _spawn_transport(runner)
    assert submitted.wait(timeout=_WATCHDOG), "transport thread did not submit"
    assert queue.pump_one() is True  # main thread runs the operation
    thread.join(timeout=_WATCHDOG)
    assert not thread.is_alive(), "transport thread stuck — cross-thread wake failed"
    assert state["error"] is None, f"unexpected error: {state['error']!r}"
    assert state["result"] == "ok"


@async_test
async def test_cross_thread_operation_exception_reaches_transport_loop() -> None:
    queue = MainThreadReadQueue()
    submitted = threading.Event()

    def boom() -> None:
        raise ValueError("houdini boom")

    async def runner() -> tuple:
        fut = queue.submit("r1", boom, deadline_monotonic=_future_deadline())
        submitted.set()
        try:
            await fut
            return ("completed", None)
        except BaseException as exc:  # noqa: BLE001
            return ("raised", exc)

    thread, state = _spawn_transport(runner)
    assert submitted.wait(timeout=_WATCHDOG)
    assert queue.pump_one() is True  # operation raises on the main thread
    thread.join(timeout=_WATCHDOG)
    assert not thread.is_alive(), "transport thread stuck on exception delivery"
    assert state["error"] is None
    kind, exc = state["result"]
    assert kind == "raised"
    assert isinstance(exc, ValueError)


@async_test
async def test_cross_thread_already_done_future_is_not_overwritten() -> None:
    queue = MainThreadReadQueue()
    future_cancelled = threading.Event()

    async def runner() -> str:
        fut = queue.submit("r1", lambda: "ok", deadline_monotonic=_future_deadline())
        fut.cancel()  # cancel the asyncio Future directly -> already done
        future_cancelled.set()
        try:
            await fut
            return "completed"
        except asyncio.CancelledError:
            return "cancelled"

    thread, state = _spawn_transport(runner)
    assert future_cancelled.wait(timeout=_WATCHDOG)
    # The operation runs, but _resolve must skip the already-done Future
    # (no InvalidStateError, no double-set).
    assert queue.pump_one() is True
    thread.join(timeout=_WATCHDOG)
    assert not thread.is_alive(), "transport thread stuck after double-set guard"
    assert state["error"] is None
    assert state["result"] == "cancelled"


@async_test
async def test_cross_thread_cancel_from_transport_thread_resolves_safely() -> None:
    queue = MainThreadReadQueue()

    async def runner() -> tuple:
        fut = queue.submit("r1", lambda: "ok", deadline_monotonic=_future_deadline())
        await asyncio.sleep(0.05)  # let it sit queued; main does NOT pump
        cancelled = queue.cancel("r1")  # called from the transport thread
        try:
            await fut
            return ("completed", cancelled)
        except QueueItemCancelled:
            return ("cancelled", cancelled)

    thread, state = _spawn_transport(runner)
    thread.join(timeout=_WATCHDOG)
    assert not thread.is_alive(), "transport thread stuck on self-cancel"
    assert state["error"] is None, f"unexpected error: {state['error']!r}"
    kind, cancelled = state["result"]
    assert cancelled is True
    assert kind == "cancelled"


@async_test
async def test_cross_thread_shutdown_wakes_transport_loop() -> None:
    queue = MainThreadReadQueue()
    submitted = threading.Event()

    async def runner() -> str:
        fut = queue.submit("r1", lambda: "ok", deadline_monotonic=_future_deadline())
        submitted.set()
        try:
            await fut
            return "completed"
        except QueueRejected:
            return "rejected"

    thread, state = _spawn_transport(runner)
    assert submitted.wait(timeout=_WATCHDOG)
    queue.shutdown()  # from the main thread; must wake the transport loop
    thread.join(timeout=_WATCHDOG)
    assert not thread.is_alive(), "transport thread stuck on shutdown drain"
    assert state["error"] is None
    assert state["result"] == "rejected"


# ==========================================================================
# Part B — read-only Houdini scene adapter (fake_hou, offline)
# ==========================================================================


class _FakeVec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._x, self._y, self._z = x, y, z

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y

    def z(self) -> float:
        return self._z


class _FakeBBox:
    def __init__(self, mn: tuple, mx: tuple) -> None:
        self._mn, self._mx = mn, mx

    def minvec(self) -> _FakeVec:
        return _FakeVec(*self._mn)

    def maxvec(self) -> _FakeVec:
        return _FakeVec(*self._mx)


class _FakeGeometry:
    def __init__(self, points: int, prims: int, bbox: _FakeBBox | None = None) -> None:
        self.points = points
        self.prims = prims
        self.bbox = bbox

    def pointCount(self) -> int:
        return self.points

    def primCount(self) -> int:
        return self.prims

    def boundingBox(self) -> _FakeBBox:
        if self.bbox is None:
            raise RuntimeError("empty geometry has no bounding box")
        return self.bbox


class _FakeNodeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeParent:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _FakeNode:
    def __init__(
        self,
        path: str,
        type_name: str,
        *,
        parent_path: str = "/",
        locked: bool = False,
        geo: _FakeGeometry | None = None,
    ) -> None:
        self._path = path
        self._type_name = type_name
        self._parent_path = parent_path
        self._locked = locked
        self._geo = geo

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _FakeNodeType:
        return _FakeNodeType(self._type_name)

    def parent(self) -> _FakeParent:
        return _FakeParent(self._parent_path)

    def isHardLocked(self) -> bool:
        return self._locked

    def isSoftLocked(self) -> bool:
        return False

    def geometry(self) -> _FakeGeometry:
        if self._geo is None:
            raise RuntimeError("node has no geometry")
        return self._geo


class _HipFileEventType:
    BeforeClear = "BeforeClear"
    AfterClear = "AfterClear"
    BeforeLoad = "BeforeLoad"
    AfterLoad = "AfterLoad"
    BeforeSave = "BeforeSave"
    AfterSave = "AfterSave"
    BeforeMerge = "BeforeMerge"
    AfterMerge = "AfterMerge"


class _FakeHipFile:
    def __init__(self, name: str = "") -> None:
        self._name = name
        self._callbacks: list[Callable[[str], None]] = []
        self.clear_called = False
        self.save_called = False

    def name(self) -> str:
        return self._name

    def set_name(self, name: str) -> None:
        self._name = name

    def addEventCallback(self, callback: Callable[[str], None]) -> None:
        self._callbacks.append(callback)

    def removeEventCallback(self, callback: Callable[[str], None]) -> None:
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def eventCallbacks(self) -> list:
        return list(self._callbacks)

    def fire(self, event_type: str) -> None:
        for cb in list(self._callbacks):
            cb(event_type)


class FakeHou:
    def __init__(
        self,
        *,
        selected: tuple[_FakeNode, ...] = (),
        nodes: dict[str, _FakeNode] | None = None,
        version: str = "21.0.440",
        hip_name: str = "",
    ) -> None:
        self._selected = selected
        self._nodes = nodes or {}
        self._version = version
        self.hipFile = _FakeHipFile(hip_name)
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
        return self._nodes.get(path)


def _geo_node(path: str = "/obj/geo1", **kw) -> _FakeNode:
    geo = _FakeGeometry(kw.get("points", 8), kw.get("prims", 6), kw.get("bbox"))
    return _FakeNode(path, "geo", parent_path="/obj", locked=kw.get("locked", False), geo=geo)


# --- 21/22/23. binding + hip path ---------------------------------------


def test_binding_from_fake_hou() -> None:
    adapter = HoudiniSceneAdapter(FakeHou(version="21.0.440", hip_name="C:/proj/scene.hip"))
    binding = adapter.binding()
    assert isinstance(binding, SceneBinding)
    assert "21.0.440" in binding.instance_id
    assert binding.scene_epoch == 1
    assert binding.hip_path == "C:/proj/scene.hip"
    assert binding.observed_revision.startswith("sha256:")
    # stable across calls
    assert adapter.binding().observed_revision == binding.observed_revision


def test_unsaved_hip_returns_none_path() -> None:
    adapter = HoudiniSceneAdapter(FakeHou(hip_name=""))
    assert adapter.binding().hip_path is None


def test_saved_hip_returns_path() -> None:
    adapter = HoudiniSceneAdapter(FakeHou(hip_name="C:/proj/scene.hip"))
    assert adapter.binding().hip_path == "C:/proj/scene.hip"


# --- 24/25/26. selection / requested nodes / geometry -------------------


def test_selected_nodes_conversion() -> None:
    node = _FakeNode("/obj/geo1", "geo", parent_path="/obj")
    adapter = HoudiniSceneAdapter(FakeHou(selected=[node]))
    result = adapter.scene_query(
        include_selection=True,
        node_paths=[],
        include_geometry_stats=False,
        expected_scene_epoch=1,
    )
    assert isinstance(result, SceneQueryResult)
    assert len(result.selected_nodes) == 1
    sn = result.selected_nodes[0]
    assert sn.path == "/obj/geo1"
    assert sn.node_type == "geo"
    assert sn.parent_path == "/obj"
    assert sn.display_name == "geo1"
    assert sn.is_locked is False
    assert sn.geometry_stats is None  # not requested


def test_requested_node_paths_conversion() -> None:
    node = _FakeNode("/obj/box1", "box", parent_path="/obj")
    adapter = HoudiniSceneAdapter(FakeHou(nodes={"/obj/box1": node}))
    result = adapter.scene_query(
        include_selection=False,
        node_paths=["/obj/box1"],
        include_geometry_stats=False,
        expected_scene_epoch=1,
    )
    assert result.selected_nodes == ()
    assert len(result.nodes) == 1
    assert result.nodes[0].path == "/obj/box1"


def test_geometry_stats_bounded_snapshot() -> None:
    node = _geo_node(points=8, prims=6, bbox=_FakeBBox((0, 0, 0), (1, 1, 1)))
    adapter = HoudiniSceneAdapter(FakeHou(selected=[node]))
    result = adapter.scene_query(
        include_selection=True,
        node_paths=[],
        include_geometry_stats=True,
        expected_scene_epoch=1,
    )
    stats = result.selected_nodes[0].geometry_stats
    assert stats is not None
    assert stats["points"] == 8
    assert stats["primitives"] == 6
    assert "bbox" in stats
    # bounded field set only — no arbitrary geometry object leakage
    assert set(stats.keys()) <= {"points", "primitives", "bbox"}


# --- 27/28. node not found / stale epoch ---------------------------------


def test_requested_node_not_found_is_structured_error() -> None:
    adapter = HoudiniSceneAdapter(FakeHou(nodes={}))
    with pytest.raises(HoudiniAdapterError) as exc:
        adapter.scene_query(
            include_selection=False,
            node_paths=["/obj/missing"],
            include_geometry_stats=False,
            expected_scene_epoch=1,
        )
    assert exc.value.code == "bridge.houdini_read_failed"


def test_stale_epoch_rejected_before_any_node_read() -> None:
    hou = FakeHou(selected=[_FakeNode("/obj/a", "geo")], nodes={"/obj/a": _FakeNode("/obj/a", "geo")})
    adapter = HoudiniSceneAdapter(hou)
    with pytest.raises(HoudiniAdapterError) as exc:
        adapter.scene_query(
            include_selection=True,
            node_paths=["/obj/a"],
            include_geometry_stats=True,
            expected_scene_epoch=99,  # current epoch is 1
        )
    assert exc.value.code == "bridge.stale_scene"
    assert exc.value.retryable is True
    # No scene read happened before the epoch rejection.
    assert hou.selected_calls == 0
    assert hou.node_calls == 0


# --- 29/30/31/32. scene epoch lifecycle ----------------------------------


def test_scene_epoch_starts_at_one() -> None:
    adapter = HoudiniSceneAdapter(FakeHou())
    assert adapter.binding().scene_epoch == 1


def test_load_increments_epoch() -> None:
    hou = FakeHou()
    adapter = HoudiniSceneAdapter(hou)
    adapter.install_scene_epoch_callbacks()
    hou.hipFile.fire(hou.hipFileEventType.AfterLoad)
    assert adapter.binding().scene_epoch == 2


def test_clear_increments_epoch() -> None:
    hou = FakeHou()
    adapter = HoudiniSceneAdapter(hou)
    adapter.install_scene_epoch_callbacks()
    hou.hipFile.fire(hou.hipFileEventType.AfterClear)
    assert adapter.binding().scene_epoch == 2


def test_save_does_not_increment_epoch() -> None:
    hou = FakeHou()
    adapter = HoudiniSceneAdapter(hou)
    adapter.install_scene_epoch_callbacks()
    hou.hipFile.fire(hou.hipFileEventType.BeforeSave)
    hou.hipFile.fire(hou.hipFileEventType.AfterSave)
    assert adapter.binding().scene_epoch == 1


# --- 33/34. immutable DTO + no proxy leak --------------------------------


def test_result_is_immutable_dto() -> None:
    node = _geo_node()
    adapter = HoudiniSceneAdapter(FakeHou(selected=[node]))
    result = adapter.scene_query(
        include_selection=True,
        node_paths=[],
        include_geometry_stats=True,
        expected_scene_epoch=1,
    )
    assert type(result.selected_nodes) is tuple
    assert type(result.nodes) is tuple
    with pytest.raises(AttributeError):
        result.binding = None  # type: ignore[misc]
    with pytest.raises(TypeError):
        result.selected_nodes[0].geometry_stats["points"] = 99  # type: ignore[index]


def test_no_hom_proxy_leaks() -> None:
    node = _geo_node()
    adapter = HoudiniSceneAdapter(FakeHou(selected=[node]))
    result = adapter.scene_query(
        include_selection=True,
        node_paths=[],
        include_geometry_stats=True,
        expected_scene_epoch=1,
    )
    assert isinstance(result.selected_nodes[0], SelectedNode)
    assert not isinstance(result.selected_nodes[0], _FakeNode)
    # The whole result must serialize to plain JSON (no proxy / fake object).
    text = json.dumps(result.to_dict(), sort_keys=True)
    assert "/obj/geo1" in text


# --- 35/36. import hygiene ----------------------------------------------


def test_adapter_does_not_import_rpyc() -> None:
    from houdini_side import secure_bridge

    mods = _module_imports(secure_bridge)
    assert "rpyc" not in mods
    assert not any("rpyc" in m for m in mods)


def test_module_import_does_not_import_hou() -> None:
    import sys

    assert "hou" not in sys.modules  # importing secure_bridge must not load hou
    from houdini_side import secure_bridge

    top_level = _module_imports(secure_bridge, top_level_only=True)
    # No module-level "import hou"; the only allowed one is inside the factory.
    assert "hou" not in top_level
    assert "hou" not in sys.modules


def test_factory_lazily_imports_hou_signature() -> None:
    source = inspect.getsource(create_houdini_scene_adapter)
    assert "import hou" in source


# --- 37/38/39. callbacks / close ----------------------------------------


def test_callback_install_is_idempotent() -> None:
    hou = FakeHou()
    adapter = HoudiniSceneAdapter(hou)
    adapter.install_scene_epoch_callbacks()
    adapter.install_scene_epoch_callbacks()
    assert len(hou.hipFile.eventCallbacks()) == 1


def test_close_is_idempotent() -> None:
    adapter = HoudiniSceneAdapter(FakeHou())
    adapter.close()
    adapter.close()  # must not raise


def test_close_does_not_modify_scene() -> None:
    hou = FakeHou(hip_name="C:/proj/scene.hip")
    adapter = HoudiniSceneAdapter(hou)
    adapter.install_scene_epoch_callbacks()
    epoch_before = adapter.binding().scene_epoch
    adapter.close()
    assert hou.hipFile.name() == "C:/proj/scene.hip"
    assert hou.hipFile.clear_called is False
    assert hou.hipFile.save_called is False
    assert adapter.binding().scene_epoch == epoch_before


# --- 40/41/42. no socket / no worker / no asyncio task -------------------


def test_adapter_creates_no_socket_or_listener() -> None:
    from houdini_side import secure_bridge

    mods = _module_imports(secure_bridge)
    assert "socket" not in mods
    assert "socketserver" not in mods
    source = inspect.getsource(secure_bridge)
    for needle in (".listen(", ".bind(", ".accept(", "socket.socket(", "create_server("):
        assert needle not in source


def test_adapter_creates_no_background_worker_or_task() -> None:
    from houdini_side import secure_bridge

    mods = _module_imports(secure_bridge)
    assert "threading" not in mods
    assert "asyncio" not in mods
    source = inspect.getsource(secure_bridge)
    for needle in ("Thread(", "create_task", "ensure_future"):
        assert needle not in source
    before = threading.active_count()
    adapter = HoudiniSceneAdapter(FakeHou())
    adapter.binding()
    adapter.close()
    assert threading.active_count() == before


# --- 43/44. queue + adapter integration ---------------------------------


@async_test
async def test_queue_and_adapter_run_in_fifo() -> None:
    node = _FakeNode("/obj/a", "geo", parent_path="/obj")
    hou = FakeHou(selected=[node], nodes={"/obj/a": node})
    adapter = HoudiniSceneAdapter(hou)
    queue = MainThreadReadQueue(capacity=4)
    order: list[str] = []

    def make_op(tag: str) -> Callable[[], SceneQueryResult]:
        def op() -> SceneQueryResult:
            order.append(tag)
            return adapter.scene_query(
                include_selection=True,
                node_paths=[],
                include_geometry_stats=False,
                expected_scene_epoch=1,
            )

        return op

    f1 = queue.submit("r1", make_op("r1"), deadline_monotonic=_future_deadline())
    f2 = queue.submit("r2", make_op("r2"), deadline_monotonic=_future_deadline())
    assert queue.pump_one() is True
    assert queue.pump_one() is True
    r1 = await f1
    r2 = await f2
    assert order == ["r1", "r2"]
    assert isinstance(r1, SceneQueryResult) and isinstance(r2, SceneQueryResult)


@async_test
async def test_all_scene_reads_happen_inside_pump() -> None:
    node = _FakeNode("/obj/a", "geo", parent_path="/obj")
    hou = FakeHou(selected=[node])
    adapter = HoudiniSceneAdapter(hou)
    queue = MainThreadReadQueue()

    def op() -> SceneQueryResult:
        return adapter.scene_query(
            include_selection=True,
            node_paths=[],
            include_geometry_stats=False,
            expected_scene_epoch=1,
        )

    fut = queue.submit("r1", op, deadline_monotonic=_future_deadline())
    assert hou.selected_calls == 0  # submit does not read the scene
    queue.pump_one()  # the read happens here, on the owner/pump thread
    assert hou.selected_calls == 1
    assert isinstance(await fut, SceneQueryResult)


# --- 45. payload mutation does not affect the snapshot -------------------


def test_scene_mutation_after_query_does_not_affect_result() -> None:
    node = _geo_node(points=8, prims=6)
    hou = FakeHou(selected=[node])
    adapter = HoudiniSceneAdapter(hou)
    result = adapter.scene_query(
        include_selection=True,
        node_paths=[],
        include_geometry_stats=True,
        expected_scene_epoch=1,
    )
    snapshot = result.selected_nodes[0].geometry_stats
    assert snapshot["points"] == 8
    # Mutate the fake scene AFTER the query — the snapshot must not change.
    node._geo.points = 9999  # type: ignore[union-attr]
    hou2_selection = [_geo_node(points=9999)]
    # the already-returned DTO is unaffected
    assert result.selected_nodes[0].geometry_stats["points"] == 8
    assert result.to_dict() == result.to_dict()  # stable
