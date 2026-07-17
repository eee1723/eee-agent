"""Task 19-A: deterministic screenshot capture executor tests.

Drives :meth:`ChangeSetExecutor.capture` directly against a ``hou``-free fake
scene supporting node create/destroy, ``parm.set``/``parmTuple.set``,
``setParmTransform``, cook/geometry bbox reads, a fake Flipbook ROP that
writes PNG bytes, ``hou.undos.group``, and failure-injection hooks, recording
every mutation into a shared spy. No ``hou``, ``rpyc``, live LLM, or real
Houdini process is used.

Covers: the happy path (verified PNG hash, atomic rename, framing report
inside the acceptance band, temp scope destroyed, scene unmutated), stale
scene / unresolvable node / cook failure / no geometry / framing budget
exhaustion (all zero-scene-change), render failure modes (raise, cook-style
errors, missing file, bad magic), an unavailable render surface, cleanup
failure (capture discarded, explicit code), and restart semantics (no
cross-instance state after an uncertain cycle).
"""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path

import pytest

from eee_agent.houdini_bridge.capture import (
    CaptureRequest,
    CaptureResult,
    CaptureSettings,
)
from houdini_side.changeset_executor import ChangeSetExecutor
from houdini_side.secure_bridge import HoudiniAdapterError, HoudiniSceneAdapter

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
ART = f"art_{'a' * 32}"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_FAKE_PNG = _PNG_MAGIC + b"\x00\x00\x00\rIHDR" + bytes(range(64))


# --------------------------------------------------------------------------
# writable fake HOM (hou-free) with create/destroy + render + failure hooks
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


class _CParm:
    def __init__(self, node: "_CNode", name: str) -> None:
        self._node = node
        self._name = name
        self._value: object = None

    def set(self, value: object) -> None:
        self._node._record("parm.set", self._name, value)
        if self._node.fail_parm_set:
            raise RuntimeError("parm.set failed")
        self._value = value

    def eval(self) -> object:
        return self._value


class _CParmTuple(_CParm):
    def set(self, value: object) -> None:
        self._node._record("parmTuple.set", self._name, tuple(value))
        if self._node.fail_parm_set:
            raise RuntimeError("parmTuple.set failed")
        self._value = tuple(value)


class _CVec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._v = (x, y, z)

    def x(self) -> float:
        return self._v[0]

    def y(self) -> float:
        return self._v[1]

    def z(self) -> float:
        return self._v[2]


class _CBBox:
    def __init__(self, mn: tuple[float, float, float], mx: tuple[float, float, float]) -> None:
        self._mn, self._mx = mn, mx

    def minvec(self) -> _CVec:
        return _CVec(*self._mn)

    def maxvec(self) -> _CVec:
        return _CVec(*self._mx)


class _CGeometry:
    def __init__(self, node: "_CNode") -> None:
        self._node = node

    def boundingBox(self) -> _CBBox:
        if self._node.bbox is None:
            raise RuntimeError("no geometry")
        return _CBBox(*self._node.bbox)


class _CNode:
    def __init__(
        self,
        scene: dict[str, "_CNode"],
        spy: list,
        path: str,
        type_name: str,
        parent: str,
        *,
        bbox: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self.bbox = bbox
        self.cook_error = ""
        self.cook_raise = False
        self.fail_parm_set = False
        self.destroy_raise = False
        self.render_raise = False
        self.render_errors = False
        self.render_no_file = False
        self.render_bad_magic = False
        self.missing_parms: set[str] = set()
        self._parms: dict[str, _CParm] = {}
        self.destroyed = False

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

    def parm(self, name: str) -> _CParm | None:
        if name in self.missing_parms:
            return None
        if name not in self._parms:
            self._parms[name] = _CParm(self, name)
        return self._parms[name]

    def parmTuple(self, name: str) -> _CParmTuple | None:
        if name in self.missing_parms:
            return None
        key = name + "#tuple"
        if key not in self._parms:
            self._parms[key] = _CParmTuple(self, name)
        return self._parms[key]

    def setParmTransform(self, matrix: object) -> None:
        self._record("setParmTransform", matrix)

    def cook(self, force: bool = False) -> None:
        self._record("cook", force)
        if self.cook_raise:
            raise RuntimeError("cook interrupted")

    def errors(self) -> tuple[str, ...]:
        if self.render_errors and self._type == "flipbook":
            return ("render failed",)
        return (self.cook_error,) if self.cook_error else ()

    def geometry(self) -> _CGeometry:
        return _CGeometry(self)

    def render(self) -> None:
        self._record("render")
        if self.render_raise:
            raise RuntimeError("render crashed")
        if self.render_errors or self.render_no_file:
            return
        picture = self._parms["picture"].eval()
        payload = _FAKE_PNG
        if self.render_bad_magic:
            payload = b"NOT-A-PNG" + bytes(32)
        Path(str(picture)).write_bytes(payload)

    def destroy(self) -> None:
        self._record("destroy")
        if self.destroy_raise:
            raise RuntimeError("destroy failed")
        self.destroyed = True
        self._scene.pop(self._path, None)

    def createNode(self, type_name: str, name: str) -> "_CNode":
        self._record("createNode", type_name, name)
        if type_name == "flipbook" and self.fail_parm_set:
            raise RuntimeError("createNode failed")
        child = _CNode(self._scene, self._spy, f"{self._path}/{name}", type_name, self._path)
        self._scene[child._path] = child
        return child


class _Root:
    def __init__(self, nodes: dict[str, _CNode]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple[_CNode, ...]:
        return tuple(self._nodes.values())


class _CWorldMatrix:
    """Fake ``hou.Matrix4`` surface: translation-only ``asTuple()``."""

    def __init__(self, translate: tuple[float, float, float]) -> None:
        self._t = translate

    def asTuple(self) -> tuple[float, ...]:
        tx, ty, tz = self._t
        return (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            tx, ty, tz, 1.0,
        )


class _CObjNode(_CNode):
    """Object-level fake: like a real ``ObjNode`` it has no ``geometry()``."""

    def __init__(
        self,
        *args: object,
        display: "_CNode | None" = None,
        world: tuple[float, float, float] = (0.0, 0.0, 0.0),
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._display = display
        self._world = world

    def geometry(self) -> _CGeometry:
        raise AttributeError("'ObjNode' object has no attribute 'geometry'")

    def displayNode(self) -> "_CNode | None":
        return self._display

    def worldTransform(self) -> _CWorldMatrix:
        return _CWorldMatrix(self._world)


class _CChildNode(_CNode):
    """SOP fake whose parent() returns the real parent node (with transform)."""

    def __init__(self, *args: object, parent_node: _CNode, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._parent_node = parent_node

    def parent(self) -> _CNode:
        return self._parent_node



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
        self.rows = tuple(tuple(row) for row in rows)  # type: ignore[union-attr]


class FakeCaptureHou:
    def __init__(self, nodes: dict[str, _CNode], spy: list) -> None:
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


# --------------------------------------------------------------------------
# factories
# --------------------------------------------------------------------------


def _standard_scene(spy: list) -> dict[str, _CNode]:
    scene: dict[str, _CNode] = {}
    scene["/obj"] = _CNode(scene, spy, "/obj", "obj", "/")
    scene["/out"] = _CNode(scene, spy, "/out", "out", "/")
    scene["/obj/ws"] = _CNode(
        scene, spy, "/obj/ws", "geo", "/obj",
        bbox=((0.0, 0.0, 0.0), (2.0, 1.0, 1.0)),
    )
    scene["/obj/ws/box1"] = _CNode(
        scene, spy, "/obj/ws/box1", "box", "/obj/ws",
        bbox=((0.0, 0.0, 0.0), (2.0, 1.0, 1.0)),
    )
    return scene


def _adapter(spy: list, scene: dict[str, _CNode] | None = None) -> HoudiniSceneAdapter:
    if scene is None:
        scene = _standard_scene(spy)
    return HoudiniSceneAdapter(FakeCaptureHou(scene, spy))


def _request(
    adapter: HoudiniSceneAdapter,
    tmp_path: Path,
    *,
    scene_epoch: int | None = None,
    node_paths: list[str] | None = None,
    settings: CaptureSettings | None = None,
    target_dir: Path | None = None,
) -> CaptureRequest:
    binding = adapter.binding()
    return CaptureRequest.build(
        request_id="req_capture",
        deadline_ms=5000,
        scene_epoch=binding.scene_epoch if scene_epoch is None else scene_epoch,
        node_paths=node_paths or ["/obj/ws/box1"],
        target_dir=str(target_dir if target_dir is not None else tmp_path),
        artifact_id=ART,
        settings=settings,
    )


def _capture_nodes(scene: dict[str, _CNode]) -> list[str]:
    return sorted(path for path in scene if "eee_capture_" in path)


# --------------------------------------------------------------------------
# happy path + evidence
# --------------------------------------------------------------------------


def test_capture_writes_hashes_renames_and_cleans_scope(tmp_path: Path) -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    result = executor.capture(_request(adapter, tmp_path))
    assert isinstance(result, CaptureResult)
    # The reference matches the delivered bytes exactly.
    final = tmp_path / f"{ART}.png"
    assert final.is_file()
    payload = final.read_bytes()
    assert payload == _FAKE_PNG
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)
    assert result.relative_path == f"{ART}.png"
    assert result.artifact_id == ART
    assert result.media_type == "image/png"
    # No leftover temp file.
    assert not (tmp_path / f".tmp_{ART}").exists()
    # The framing report sits inside the deterministic acceptance band.
    framing = result.framing
    assert 0 <= framing.adjustments_used <= 2
    assert framing.margin_left >= 0.06
    assert framing.margin_right >= 0.06
    assert framing.margin_bottom >= 0.06
    assert framing.margin_top >= 0.06
    assert 0.72 <= framing.longest_axis_ratio <= 0.84
    assert framing.center_offset <= 0.03
    # The owned temp scope is gone; the scene kept only the original nodes.
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]
    assert ("undo_group_begin", "EEE Agent - capture.capture") in spy
    assert ("undo_group_end", "EEE Agent - capture.capture") in spy
    # The deterministic neutral render settings reached the ROP.
    rop_path = "/out/eee_capture_" + ART[4:]
    rop_sets = [m for m in spy if m[0] == "parm.set" and m[1] == rop_path]
    assert ("parm.set", rop_path, "aamode", "aa8") in rop_sets
    assert ("parm.set", rop_path, "shadingmode", "smooth") in rop_sets
    assert ("parm.set", rop_path, "lighting", "headlight") in rop_sets
    assert ("parm.set", rop_path, "usematerials", 0) in rop_sets
    assert ("parm.set", rop_path, "usetextures", 0) in rop_sets
    res_sets = [m for m in spy if m[0] == "parmTuple.set" and m[2] == "res"]
    assert res_sets == [("parmTuple.set", rop_path, "res", (1280, 960))]


def test_capture_render_target_uses_forward_slashes(tmp_path: Path) -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    executor.capture(_request(adapter, tmp_path))
    pictures = [m for m in spy if m[0] == "parm.set" and m[2] == "picture"]
    assert len(pictures) == 1
    assert "\\" not in pictures[0][3]
    assert pictures[0][3].endswith(f"/.tmp_{ART}/{ART}.png")


def test_capture_object_level_node_frames_world_space_bbox(tmp_path: Path) -> None:
    # An object-level node has no geometry(): the capture must read its
    # display SOP geometry and lift the bbox by the object world transform.
    spy: list = []
    scene = _standard_scene(spy)
    display = scene["/obj/ws/box1"]
    scene["/obj/ws"] = _CObjNode(
        scene, spy, "/obj/ws", "geo", "/obj",
        display=display, world=(10.0, 0.0, 0.0),
    )
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    result = executor.capture(_request(adapter, tmp_path, node_paths=["/obj/ws"]))
    assert isinstance(result, CaptureResult)
    framing = result.framing
    assert 0 <= framing.adjustments_used <= 2
    assert framing.margin_left >= 0.06
    assert framing.margin_right >= 0.06
    assert 0.72 <= framing.longest_axis_ratio <= 0.84
    assert framing.center_offset <= 0.03
    # The camera framed the translated box (world center x=11), not the origin.
    cam_sets = [m for m in spy if m[0] == "setParmTransform"]
    assert len(cam_sets) == 1
    rows = cam_sets[0][2].rows
    assert rows[0][3] > 10.0
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]


def test_capture_sop_bbox_is_lifted_by_parent_world_transform(tmp_path: Path) -> None:
    # A SOP-level evidence node is framed in world space via its parent
    # object transform when the parent exposes worldTransform().
    spy: list = []
    scene = _standard_scene(spy)
    obj = _CObjNode(
        scene, spy, "/obj/ws", "geo", "/obj",
        display=None, world=(10.0, 0.0, 0.0),
    )
    scene["/obj/ws"] = obj
    scene["/obj/ws/box1"] = _CChildNode(
        scene, spy, "/obj/ws/box1", "box", "/obj/ws",
        parent_node=obj, bbox=((0.0, 0.0, 0.0), (2.0, 1.0, 1.0)),
    )
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    result = executor.capture(_request(adapter, tmp_path, node_paths=["/obj/ws/box1"]))
    assert isinstance(result, CaptureResult)
    cam_sets = [m for m in spy if m[0] == "setParmTransform"]
    assert len(cam_sets) == 1
    rows = cam_sets[0][2].rows
    assert rows[0][3] > 10.0
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# pre-scope fail-closed paths (zero scene changes)
# --------------------------------------------------------------------------


def test_capture_stale_scene_performs_zero_changes(tmp_path: Path) -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, tmp_path, scene_epoch=adapter.binding().scene_epoch + 1)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(request)
    assert exc.value.code == "bridge.stale_scene"
    assert exc.value.retryable is True
    assert [m for m in spy if m[0] in ("createNode", "cook", "render")] == []


def test_capture_missing_target_dir_performs_zero_changes(tmp_path: Path) -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, tmp_path, target_dir=tmp_path / "ghost")
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(request)
    assert exc.value.code == "capture.target_unavailable"
    assert exc.value.retryable is False
    assert [m for m in spy if m[0] in ("createNode", "cook", "render")] == []


def test_capture_unknown_node_performs_zero_changes(tmp_path: Path) -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    request = _request(adapter, tmp_path, node_paths=["/obj/ws/ghost"])
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(request)
    assert exc.value.code == "capture.invalid"
    assert exc.value.retryable is False
    assert [m for m in spy if m[0] in ("createNode", "cook", "render")] == []


def test_capture_cook_failure_performs_zero_changes(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].cook_error = "cook failed"
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.cook_failed"
    assert exc.value.retryable is True
    assert [m for m in spy if m[0] in ("createNode", "render")] == []


def test_capture_without_geometry_fails_closed(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/box1"].bbox = None
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.no_geometry"
    assert exc.value.retryable is False
    assert [m for m in spy if m[0] in ("createNode", "render")] == []


def test_capture_framing_budget_exhaustion_fails_closed(tmp_path: Path) -> None:
    # The off-axis box needs one recenter adjustment; a zero-adjustment
    # request makes the acceptance band unreachable and must fail closed.
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter)
    settings = CaptureSettings(max_adjustments=0)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path, settings=settings))
    assert exc.value.code == "capture.framing_failed"
    assert exc.value.retryable is False
    assert [m for m in spy if m[0] in ("createNode", "render")] == []
    assert not (tmp_path / f"{ART}.png").exists()


# --------------------------------------------------------------------------
# render-surface and render failure modes: scope cleaned, classified error
# --------------------------------------------------------------------------


def test_capture_missing_render_parm_cleans_scope_and_fails(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.missing_parms.add("lighting")
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.render_unavailable"
    assert exc.value.retryable is False
    # The temp scope was still destroyed; no capture file was left behind.
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]
    assert not (tmp_path / f"{ART}.png").exists()
    assert not (tmp_path / f".tmp_{ART}").exists()


def test_capture_render_raise_cleans_scope_and_fails(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.render_raise = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.render_failed"
    assert exc.value.retryable is True
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]
    assert not (tmp_path / f"{ART}.png").exists()


def test_capture_render_errors_cleans_scope_and_fails(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.render_errors = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.render_failed"
    assert _capture_nodes(adapter._hou._nodes) == []  # type: ignore[attr-defined]


def test_capture_missing_output_file_fails_closed(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.render_no_file = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.render_failed"
    assert not (tmp_path / f"{ART}.png").exists()
    assert not (tmp_path / f".tmp_{ART}").exists()


def test_capture_bad_magic_output_fails_closed(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.render_bad_magic = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.render_failed"
    # The invalid bytes were removed; nothing was renamed into place.
    assert not (tmp_path / f"{ART}.png").exists()
    assert not (tmp_path / f".tmp_{ART}").exists()


# --------------------------------------------------------------------------
# cleanup failure: capture discarded, explicit code, never a guessed success
# --------------------------------------------------------------------------


def test_capture_cleanup_failure_discards_capture(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.destroy_raise = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.capture(_request(adapter, tmp_path))
    assert exc.value.code == "capture.cleanup_failed"
    assert exc.value.retryable is False
    # The rendered bytes were discarded with the capture; no artifact remains.
    assert not (tmp_path / f"{ART}.png").exists()
    assert not (tmp_path / f".tmp_{ART}").exists()
    # The temp ROP is still in the scene (the cleanup failure is the point).
    assert _capture_nodes(adapter._hou._nodes) != []  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# restart: no cross-instance state after an uncertain cycle
# --------------------------------------------------------------------------


def test_capture_restart_performs_no_replay_and_starts_clean(tmp_path: Path) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    out = scene["/out"]
    adapter = _adapter(spy, scene)
    executor = ChangeSetExecutor(adapter)
    original_create = out.createNode

    def _create(type_name: str, name: str) -> _CNode:
        node = original_create(type_name, name)
        node.render_raise = True
        return node

    out.createNode = _create  # type: ignore[method-assign]
    with pytest.raises(HoudiniAdapterError):
        executor.capture(_request(adapter, tmp_path))

    # Restart idiom: a new process gets a new executor with no replay state.
    restarted_spy: list = []
    restarted_adapter = _adapter(restarted_spy)
    restarted_executor = ChangeSetExecutor(restarted_adapter)
    result = restarted_executor.capture(_request(restarted_adapter, tmp_path))
    assert result.sha256 == hashlib.sha256(_FAKE_PNG).hexdigest()
    assert _capture_nodes(restarted_adapter._hou._nodes) == []  # type: ignore[attr-defined]
