"""Task 18-F: transactional sensitivity sample-and-restore executor tests.

Drives :meth:`ChangeSetExecutor.sample_sensitivity` directly against a
``hou``-free WRITABLE fake scene supporting ``parm.set``/``cook``/``errors``/
``geometry``/``hou.undos.group`` plus failure-injection hooks, recording every
mutation into a shared spy. No ``hou``, ``rpyc``, live LLM, or real Houdini
process is used.

Covers: exact restore with read-back verification and evidence digests,
stale-scene zero-write refusal, cook failure with a verified restore, restore
failure with the write freeze, queue cancellation mid-sample (interruption
discards the result but the scene is still restored), and restart semantics
(a fresh executor performs no replay of the prior uncertain cycle).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time

import pytest

from eee_agent.houdini_bridge.queue import MainThreadReadQueue, QueueItemCancelled
from eee_agent.houdini_bridge.sensitivity import (
    SensitivitySampleRequest,
    SensitivitySampleTarget,
)
from eee_agent.modeling.validation import _geometry_evidence_digest
from houdini_side.changeset_executor import ChangeSetExecutor
from houdini_side.secure_bridge import HoudiniAdapterError, HoudiniSceneAdapter

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"


# --------------------------------------------------------------------------
# writable fake HOM (hou-free) with cook/geometry + failure-injection hooks
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


class _SParm:
    def __init__(self, node: "_SNode", name: str, value: object) -> None:
        self._node = node
        self._name = name
        self._value = value
        # Restore-failure injection: the write-back silently does not take.
        self.sticky_after_first_set = False
        self._set_count = 0

    def eval(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._node._record("parm.set", self._name, value)
        self._set_count += 1
        if self._node.fail_set:
            raise RuntimeError("parm.set failed")
        if self.sticky_after_first_set and self._set_count > 1:
            return
        self._value = value


class _SVec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._v = (x, y, z)

    def x(self) -> float:
        return self._v[0]

    def y(self) -> float:
        return self._v[1]

    def z(self) -> float:
        return self._v[2]


class _SBBox:
    def __init__(self, mx: float) -> None:
        self._mx = mx

    def minvec(self) -> _SVec:
        return _SVec(0.0, 0.0, 0.0)

    def maxvec(self) -> _SVec:
        return _SVec(self._mx, 1.0, 1.0)


class _SGeometry:
    """Geometry facts derived from the node's numeric parms (cook evidence)."""

    def __init__(self, node: "_SNode") -> None:
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

    def boundingBox(self) -> _SBBox:
        return _SBBox(float(self._points()))


class _SNode:
    def __init__(
        self,
        scene: dict[str, "_SNode"],
        spy: list,
        path: str,
        type_name: str,
        parent: str,
        *,
        user_data: dict[str, str] | None = None,
        parms: dict[str, object] | None = None,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self._user_data = dict(user_data or {})
        self._parms = {n: _SParm(self, n, v) for n, v in (parms or {}).items()}
        self.fail_set = False
        self.cook_error = ""
        self.cook_raise = False
        # Interruption barrier: block inside cook() so a cross-thread test can
        # cancel the running queue item mid-cycle.
        self.cook_enter_event: threading.Event | None = None
        self.cook_release_event: threading.Event | None = None

    def _record(self, method: str, *args: object) -> None:
        self._spy.append((method, self._path, *args))

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
        self._record("setUserData", key)
        self._user_data[key] = value

    def parm(self, name: str) -> _SParm | None:
        return self._parms.get(name)

    def parmTuple(self, name: str) -> None:
        return None

    def isHardLocked(self) -> bool:
        return False

    def isSoftLocked(self) -> bool:
        return False

    def cook(self, force: bool = False) -> None:
        self._record("cook", force)
        if self.cook_enter_event is not None:
            self.cook_enter_event.set()
        if self.cook_release_event is not None:
            self.cook_release_event.wait(timeout=10)
        if self.cook_raise:
            raise RuntimeError("cook interrupted")

    def errors(self) -> tuple[str, ...]:
        return (self.cook_error,) if self.cook_error else ()

    def geometry(self) -> _SGeometry:
        return _SGeometry(self)


class _Root:
    def __init__(self, nodes: dict[str, _SNode]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple[_SNode, ...]:
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


class FakeSampleHou:
    def __init__(self, nodes: dict[str, _SNode], spy: list) -> None:
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


# --------------------------------------------------------------------------
# factories
# --------------------------------------------------------------------------


def _mirror(node_id: str, role: str = "member") -> dict[str, str]:
    return {
        "eee.workspace_id": WS, "eee.node_id": node_id, "eee.capability": "modeling",
        "eee.role": role, "eee.schema_version": "1", "eee.created_by_run": RUN,
    }


def _standard_scene(spy: list) -> dict[str, _SNode]:
    scene: dict[str, _SNode] = {}
    root = _SNode(scene, spy, "/obj/ws", "geo", "/obj", user_data=_mirror("n_root", role="root"))
    box = _SNode(
        scene, spy, "/obj/ws/box1", "box", "/obj/ws",
        user_data=_mirror("n_box"), parms={"sizex": 2.0},
    )
    scene["/obj/ws"] = root
    scene["/obj/ws/box1"] = box
    return scene


def _adapter(spy: list, scene: dict[str, _SNode] | None = None) -> HoudiniSceneAdapter:
    if scene is None:
        scene = _standard_scene(spy)
    return HoudiniSceneAdapter(FakeSampleHou(scene, spy))


def _target(**overrides: object) -> SensitivitySampleTarget:
    values: dict[str, object] = {
        "node_id": "n_box",
        "path": "/obj/ws/box1",
        "parm_name": "sizex",
        "value": 3.0,
    }
    values.update(overrides)
    return SensitivitySampleTarget(**values)  # type: ignore[arg-type]


def _request(
    adapter: HoudiniSceneAdapter,
    *,
    scene_epoch: int | None = None,
    samples: list[SensitivitySampleTarget] | None = None,
    node_paths: list[str] | None = None,
    request_id: str = "req_sample",
) -> SensitivitySampleRequest:
    binding = adapter.binding()
    return SensitivitySampleRequest.build(
        request_id=request_id,
        deadline_ms=5000,
        scene_epoch=binding.scene_epoch if scene_epoch is None else scene_epoch,
        node_paths=node_paths or ["/obj/ws", "/obj/ws/box1"],
        samples=samples if samples is not None else [_target()],
    )


# --------------------------------------------------------------------------
# happy path + evidence
# --------------------------------------------------------------------------


def test_sample_cycle_restores_exact_value_and_returns_evidence() -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    result = executor.sample_sensitivity(_request(adapter))
    # Evidence shape: one baseline, one sample per target, one restored query,
    # all bound to the same scene epoch.
    assert len(result.samples) == 1
    assert result.baseline.binding.scene_epoch == adapter.binding().scene_epoch
    assert result.restored.binding.scene_epoch == result.baseline.binding.scene_epoch
    # The sample changed the geometry evidence; the restore returned exactly
    # to the baseline evidence.
    baseline_digest = _geometry_evidence_digest(result.baseline)
    assert _geometry_evidence_digest(result.samples[0]) != baseline_digest
    assert _geometry_evidence_digest(result.restored) == baseline_digest
    # The parameter itself was restored exactly (write 3.0 then write-back 2.0).
    box = adapter._hou.node("/obj/ws/box1")
    assert box.parm("sizex").eval() == 2.0
    writes = [m for m in spy if m[0] == "parm.set"]
    assert writes == [
        ("parm.set", "/obj/ws/box1", "sizex", 3.0),
        ("parm.set", "/obj/ws/box1", "sizex", 2.0),
    ]
    # One undo group wraps the sample writes.
    assert ("undo_group_begin", "EEE Agent - sensitivity.sample") in spy
    assert ("undo_group_end", "EEE Agent - sensitivity.sample") in spy
    assert executor.write_frozen is False


def test_sample_resolves_target_by_stable_node_id() -> None:
    # The sample write follows the mirrored stable id even at a moved path:
    # the request path is stale, but the id resolves to the current node.
    spy: list = []
    scene = _standard_scene(spy)
    moved = _SNode(
        scene, spy, "/obj/ws/box1_moved", "box", "/obj/ws",
        user_data=_mirror("n_box"), parms={"sizex": 2.0},
    )
    scene["/obj/ws/box1_moved"] = moved
    del scene["/obj/ws/box1"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    result = executor.sample_sensitivity(
        _request(adapter, node_paths=["/obj/ws/box1_moved"])
    )
    assert len(result.samples) == 1
    assert moved.parm("sizex").eval() == 2.0
    writes = [m for m in spy if m[0] == "parm.set"]
    assert writes[0][1] == "/obj/ws/box1_moved"


# --------------------------------------------------------------------------
# pre-write fail-closed paths (zero writes)
# --------------------------------------------------------------------------


def test_sample_stale_scene_performs_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, scene_epoch=adapter.binding().scene_epoch + 1)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(request)
    assert exc.value.code == "bridge.stale_scene"
    assert exc.value.retryable is True
    assert [m for m in spy if m[0] == "parm.set"] == []
    assert [m for m in spy if m[0] == "undo_group_begin"] == []
    assert executor.write_frozen is False


def test_sample_unknown_target_performs_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, samples=[_target(node_id=None, path="/obj/ws/ghost")])
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(request)
    assert exc.value.code == "sensitivity.invalid"
    assert [m for m in spy if m[0] == "parm.set"] == []
    assert executor.write_frozen is False


def test_sample_missing_parameter_performs_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, samples=[_target(parm_name="ghostparm")])
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(request)
    assert exc.value.code == "sensitivity.invalid"
    assert [m for m in spy if m[0] == "parm.set"] == []
    assert executor.write_frozen is False


# --------------------------------------------------------------------------
# cook failure: restore runs, classified error, no freeze, no result
# --------------------------------------------------------------------------


def test_sample_cook_failure_restores_exactly_and_fails_closed() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].cook_error = "cook failed"
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(_request(adapter))
    assert exc.value.code == "sensitivity.cook_failed"
    assert exc.value.retryable is True
    # The sample write was restored exactly even though the cook failed.
    assert scene["/obj/ws/box1"].parm("sizex").eval() == 2.0
    writes = [m for m in spy if m[0] == "parm.set"]
    assert writes == [
        ("parm.set", "/obj/ws/box1", "sizex", 3.0),
        ("parm.set", "/obj/ws/box1", "sizex", 2.0),
    ]
    # A verified restore means the scene is clean: no freeze, no result.
    assert executor.write_frozen is False


def test_sample_cook_interruption_restores_exactly_and_fails_closed() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].cook_raise = True
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(_request(adapter))
    assert exc.value.code == "sensitivity.cook_failed"
    assert scene["/obj/ws/box1"].parm("sizex").eval() == 2.0
    assert executor.write_frozen is False


# --------------------------------------------------------------------------
# restore failure: freeze writes, never a guessed success
# --------------------------------------------------------------------------


def test_sample_restore_failure_freezes_writes() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].parm("sizex").sticky_after_first_set = True
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.sample_sensitivity(_request(adapter))
    assert exc.value.code == "sensitivity.restore_failed"
    assert exc.value.retryable is False
    # The write-back verification proved the scene unrestored: writes freeze.
    assert executor.write_frozen is True
    # A later sample on the frozen executor fails closed with zero new writes.
    writes_before = len([m for m in spy if m[0] == "parm.set"])
    with pytest.raises(HoudiniAdapterError) as exc2:
        executor.sample_sensitivity(_request(adapter))
    assert exc2.value.code == "bridge.write_frozen"
    assert len([m for m in spy if m[0] == "parm.set"]) == writes_before


# --------------------------------------------------------------------------
# interruption: queue cancellation mid-cycle (F8 idiom)
# --------------------------------------------------------------------------


def test_sample_cancellation_mid_cycle_discards_result_but_restores() -> None:
    """A transport-side cancel landing mid-cycle discards the result; the
    sample-and-restore cycle still runs to completion on the pump thread, so
    the scene is verifiably restored and never left sampled."""
    spy: list = []
    scene = _standard_scene(spy)
    enter = threading.Event()
    release = threading.Event()
    scene["/obj/ws/box1"].cook_enter_event = enter
    scene["/obj/ws/box1"].cook_release_event = release
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    queue = MainThreadReadQueue()
    request = _request(adapter)

    async def scenario() -> None:
        fut = queue.submit(
            request.request_id,
            lambda: executor.sample_sensitivity(request),
            deadline_monotonic=time.monotonic() + 30,
        )
        pump_done = threading.Event()

        def _pump() -> None:
            queue.pump_one()
            pump_done.set()

        t = threading.Thread(target=_pump)
        t.start()
        # The cycle is now inside the sample cook (after the first write).
        assert enter.wait(timeout=10), "operation did not reach the sample cook"
        assert queue.cancel(request.request_id) is True
        release.set()
        assert pump_done.wait(timeout=10)
        t.join(timeout=10)
        # The waiter receives QueueItemCancelled (evidence discarded).
        with pytest.raises(QueueItemCancelled):
            await fut
        # The cycle DID complete: the parameter was restored exactly.
        assert scene["/obj/ws/box1"].parm("sizex").eval() == 2.0
        writes = [m for m in spy if m[0] == "parm.set"]
        assert writes == [
            ("parm.set", "/obj/ws/box1", "sizex", 3.0),
            ("parm.set", "/obj/ws/box1", "sizex", 2.0),
        ]
        assert executor.write_frozen is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# restart: no replay of the prior uncertain cycle
# --------------------------------------------------------------------------


def test_sample_restart_performs_no_replay_and_starts_clean() -> None:
    """Sampling caches no durable state, so restart recovery has nothing to
    replay: the process-local write freeze (exactly the apply uncertainty
    rule) is the only carry-over, and a fresh executor over a fresh scene
    starts unfrozen and runs a full new cycle."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].parm("sizex").sticky_after_first_set = True
    adapter = _adapter(spy, scene)
    frozen_executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError):
        frozen_executor.sample_sensitivity(_request(adapter))
    assert frozen_executor.write_frozen is True

    # Restart idiom: a new process gets a new executor with no replay state.
    restarted_spy: list = []
    restarted_adapter = _adapter(restarted_spy)
    restarted_executor = ChangeSetExecutor(restarted_adapter)
    assert restarted_executor.write_frozen is False
    result = restarted_executor.sample_sensitivity(_request(restarted_adapter))
    assert len(result.samples) == 1
    assert restarted_adapter._hou.node("/obj/ws/box1").parm("sizex").eval() == 2.0
    # The frozen executor stays frozen; no cross-instance state leaks.
    assert frozen_executor.write_frozen is True
