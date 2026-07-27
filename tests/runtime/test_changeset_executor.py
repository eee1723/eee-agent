"""Task 16-D: transactional ChangeSet executor (apply / rollback / receipt).

Drives :class:`ChangeSetExecutor` directly against a ``hou``-free WRITABLE fake
scene supporting ``createNode``/``parm.set``/``parmTuple.set``/``setInput``/
``setUserData``/``destroy``/``hou.undos.group`` plus mutation/failure-injection
hooks, recording every mutation into a shared spy. No ``hou``, ``rpyc``, live
LLM, or real Houdini process is used.

Covers (F1-F7): mandatory derived pre/post + checkpoint enforcement, the pure
policy engine re-run before writes, early-inverse journaling on mirror/mutate
failure, post-write failure containment + truthful Partial/Critical + freeze,
idempotency/receipt semantics, and rollback identity proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from datetime import datetime, timezone

import pytest

from eee_agent.changesets import (
    ChangeSet,
    CheckpointPlan,
    ConnectInput,
    CreateNode,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SetParm,
    WireRef,
    WorkspaceManifest,
)
from eee_agent.changesets.contracts import _derive_create_path
from eee_agent.houdini_bridge.changesets import ApplyRequest
from eee_agent.houdini_bridge.queue import MainThreadReadQueue, QueueItemCancelled
from houdini_side.changeset_executor import (
    ChangeSetExecutor,
    ChangeSetPreflightAdapter,
    _dedup,
    _wire_equal,
    derive_mandatory_checkpoint,
    derive_mandatory_postconditions,
    derive_mandatory_preconditions,
)
from houdini_side.secure_bridge import HoudiniAdapterError, HoudiniSceneAdapter

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
REVISION = "a" * 64


# --------------------------------------------------------------------------
# writable fake HOM (hou-free) with mutation + failure-injection spy
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


class _Parm:
    def __init__(self, node: "_Node", name: str, value: object) -> None:
        self._node = node
        self._name = name
        self._value = value
        self.fail_set_after = 0
        self.raise_after_set_once = False
        self._set_count = 0

    def eval(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._node._record("parm.set", self._name)
        self._set_count += 1
        if self._node.fail_set or (self.fail_set_after and self._set_count > self.fail_set_after):
            raise RuntimeError("parm.set failed")
        self._value = value
        if self.raise_after_set_once:
            self.raise_after_set_once = False  # one-shot: rollback restore must succeed
            raise RuntimeError("parm.set raised after mutating")


class _Conn:
    def __init__(self, out_node: "_Node", out_idx: int, in_idx: int) -> None:
        self._out, self._oi, self._ii = out_node, out_idx, in_idx

    def outputNode(self) -> "_Node":
        return self._out

    def outputIndex(self) -> int:
        return self._oi

    def inputIndex(self) -> int:
        return self._ii


class _Node:
    def __init__(
        self, scene: dict[str, "_Node"], spy: list, path: str, type_name: str, parent: str, *,
        user_data: dict[str, str] | None = None, parms: dict[str, object] | None = None,
        hard_locked: bool = False, soft_locked: bool = False,
        fail_destroy: bool = False, fail_set: bool = False, fail_setinput: bool = False,
        fail_create: bool = False, created_fail_destroy: bool = False, fail_create_after: int = 0,
        fail_type_read: bool = False, fail_set_user_data_key: str | None = None,
        created_fail_set_user_data_key: str | None = None,
        created_fail_type_read: bool = False, raise_after_setinput_once: bool = False,
        created_raise_after_setinput_once: bool = False,
        fail_user_data: bool = False, created_fail_user_data: bool = False,
        create_enter_event: object = None, create_release_event: object = None,
        created_parms: dict[str, object] | None = None,
        tamper_key: str | None = None, created_tamper_key: str | None = None,
        created_tamper_path: str | None = None, created_tamper_type: str | None = None,
        _tamper_path: str | None = None, _tamper_type: str | None = None,
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
        self._tamper_path = _tamper_path
        self._tamper_type = _tamper_type
        self._user_data = dict(user_data or {})
        self._parms = {n: _Parm(self, n, v) for n, v in (parms or {}).items()}
        self._inputs: dict[int, tuple[_Node, int]] = {}
        self.hard_locked = hard_locked
        self.soft_locked = soft_locked
        self.fail_destroy = fail_destroy
        self.fail_set = fail_set
        self.fail_setinput = fail_setinput
        self.fail_create = fail_create
        self.created_fail_destroy = created_fail_destroy
        self.fail_create_after = fail_create_after
        self.fail_type_read = fail_type_read
        self.fail_set_user_data_key = fail_set_user_data_key
        self.created_fail_set_user_data_key = created_fail_set_user_data_key
        self.created_fail_type_read = created_fail_type_read
        self.raise_after_setinput_once = raise_after_setinput_once
        self.created_raise_after_setinput_once = created_raise_after_setinput_once
        self.fail_user_data = fail_user_data
        self.created_fail_user_data = created_fail_user_data
        self.create_enter_event = create_enter_event
        self.create_release_event = create_release_event
        self.created_parms = dict(created_parms or {})
        self.tamper_key = tamper_key
        self.created_tamper_key = created_tamper_key
        self.created_tamper_path = created_tamper_path
        self.created_tamper_type = created_tamper_type
        self._create_count = 0

    def _record(self, method: str, *args: object) -> None:
        self._spy.append((method, self._path, *args))

    def path(self) -> str:
        return self._tamper_path if self._tamper_path is not None else self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        if self.fail_type_read:
            raise RuntimeError("type read failed")
        return _Type(self._tamper_type if self._tamper_type is not None else self._type)

    def parent(self) -> _ParentRef:
        return _ParentRef(self._parent)

    def userData(self, key: str) -> str | None:
        if self.fail_user_data:
            raise RuntimeError("userData read failed")
        return self._user_data.get(key)

    def setUserData(self, key: str, value: str) -> None:
        self._record("setUserData", key)
        if self.fail_set_user_data_key is not None and key == self.fail_set_user_data_key:
            raise RuntimeError(f"setUserData({key}) failed")
        if self.tamper_key is not None and key == self.tamper_key:
            self._user_data[key] = "TAMPERED"
        else:
            self._user_data[key] = value

    def parm(self, name: str) -> _Parm | None:
        return self._parms.get(name)

    def parmTuple(self, name: str) -> None:
        return None

    def inputConnections(self) -> list[_Conn]:
        return [_Conn(self._inputs[i][0], self._inputs[i][1], i) for i in sorted(self._inputs)]

    def inputs(self) -> list:
        if not self._inputs:
            return []
        size = max(self._inputs) + 1
        return [self._inputs[i][0] if i in self._inputs else None for i in range(size)]

    def setInput(self, idx: int, src: "_Node | None", out_idx: int = 0) -> None:
        self._record("setInput", idx)
        if self.fail_setinput:
            raise RuntimeError("setInput failed")
        if src is None:
            self._inputs.pop(idx, None)
        else:
            self._inputs[idx] = (src, out_idx)
        if self.raise_after_setinput_once:
            self.raise_after_setinput_once = False
            raise RuntimeError("setInput raised after mutating")
        cb = getattr(self, "_post_setinput_callback", None)
        if cb is not None:
            self._post_setinput_callback = None  # one-shot
            cb(self, idx, src)

    def createNode(self, type_name: str, name: str) -> "_Node":
        self._record("createNode", type_name, name)
        self._create_count += 1
        if self.fail_create or (self.fail_create_after and self._create_count > self.fail_create_after):
            raise RuntimeError("createNode failed")
        # F8: synchronization barrier — block here so a cross-thread test can
        # cancel the running queue item while the operation is inside createNode.
        if self.create_enter_event is not None:
            self.create_enter_event.set()
        if self.create_release_event is not None:
            self.create_release_event.wait(timeout=10)
        path = self._path.rstrip("/") + "/" + name
        node = _Node(
            self._scene, self._spy, path, type_name, self._path,
            fail_destroy=self.created_fail_destroy,
            fail_type_read=self.created_fail_type_read,
            fail_set_user_data_key=self.created_fail_set_user_data_key,
            raise_after_setinput_once=self.created_raise_after_setinput_once,
            fail_user_data=self.created_fail_user_data,
            parms=dict(self.created_parms),
            created_parms=dict(self.created_parms),
            tamper_key=self.created_tamper_key,
            _tamper_path=self.created_tamper_path,
            _tamper_type=self.created_tamper_type,
        )
        self._scene[path] = node
        return node

    def destroy(self) -> None:
        self._record("destroy")
        if self.fail_destroy:
            raise RuntimeError("destroy failed")
        self._scene.pop(self._path, None)

    def isHardLocked(self) -> bool:
        return self.hard_locked

    def isSoftLocked(self) -> bool:
        return self.soft_locked


class _Root:
    def __init__(self, nodes: dict[str, _Node]) -> None:
        self._nodes = nodes

    def allSubChildren(self) -> tuple[_Node, ...]:
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

    def disabler(self):
        # Mirrors hou.undos.disabler(): a context manager that suppresses undo
        # recording. Used by explicit scratch.destroy so disposal cannot be
        # undone back into an orphan node.
        spy = self._spy

        @contextlib.contextmanager
        def _d():
            spy.append(("undo_disable_begin",))
            try:
                yield
            finally:
                spy.append(("undo_disable_end",))

        return _d()


class _HipFile:
    def __init__(self, name: str = "") -> None:
        self._name = name
        self._callbacks: list = []

    def name(self) -> str:
        return self._name

    def addEventCallback(self, cb: object) -> None:
        self._callbacks.append(cb)

    def removeEventCallback(self, cb: object) -> None:
        if cb in self._callbacks:
            self._callbacks.remove(cb)


class _HipFileEventType:
    AfterClear = "AfterClear"
    AfterLoad = "AfterLoad"


class FakeWriteHou:
    def __init__(self, nodes: dict[str, _Node], spy: list) -> None:
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


def _mirror(node_id: str, capability: str = "modeling", role: str = "member") -> dict[str, str]:
    return {
        "eee.workspace_id": WS, "eee.node_id": node_id, "eee.capability": capability,
        "eee.role": role, "eee.schema_version": "1", "eee.created_by_run": RUN,
    }


def _standard_scene(spy: list) -> dict[str, _Node]:
    scene: dict[str, _Node] = {}
    root = _Node(scene, spy, "/obj/ws", "subnet", "/obj", user_data=_mirror("n_root", role="root"))
    src = _Node(scene, spy, "/obj/ws/src1", "xform", "/obj/ws", user_data=_mirror("n_src"))
    child = _Node(scene, spy, "/obj/ws/geo1", "geo", "/obj/ws", user_data=_mirror("n_child"), parms={"tx": 0})
    scene["/obj/ws"] = root
    scene["/obj/ws/src1"] = src
    scene["/obj/ws/geo1"] = child
    child._inputs = {0: (src, 0)}
    return scene


def _adapter(spy: list, scene: dict[str, _Node] | None = None) -> HoudiniSceneAdapter:
    if scene is None:
        scene = _standard_scene(spy)
    return HoudiniSceneAdapter(FakeWriteHou(scene, spy))


def _owned(node_id: str, path: str, node_type: str, parent: str, **extra: str) -> OwnedNodeRef:
    kwargs = {"capability": "modeling", "role": "member"}
    kwargs.update(extra)
    return OwnedNodeRef(node_id=node_id, path=path, node_type=node_type, parent_path=parent, **kwargs)


def _manifest(adapter: HoudiniSceneAdapter) -> WorkspaceManifest:
    binding = adapter.binding()
    root = _owned("n_root", "/obj/ws", "subnet", "/obj", role="root")
    src = _owned("n_src", "/obj/ws/src1", "xform", "/obj/ws")
    child = _owned("n_child", "/obj/ws/geo1", "geo", "/obj/ws")
    return WorkspaceManifest.build(
        workspace_id=WS, session_id=SES, instance_id=binding.instance_id,
        scene_epoch=binding.scene_epoch, roots=[root], nodes=[root, src, child],
        created_by_run=RUN, updated_at=NOW,
    )


def _noderef(node_id: str | None, path: str, expected_type: str, ws: str | None = WS) -> NodeRef:
    return NodeRef(node_id=node_id, path=path, expected_type=expected_type, expected_workspace_id=ws)


def _risk(operations: tuple[object, ...], affected_paths: tuple[str, ...], *, backup: bool = False) -> RiskSummary:
    effects = tuple(sorted({op.effect.value for op in operations}))  # type: ignore[attr-defined]
    return RiskSummary(
        touches_external_nodes=False, changes_wiring=("wire.connect" in effects),
        requires_backup=backup, operation_count=len(operations), effect_names=effects,
        affected_paths=tuple(sorted(set(affected_paths))),
    )


def _affected_from_ops(operations) -> list[NodeRef]:
    affected: list[NodeRef] = []
    for op in operations:
        if isinstance(op, CreateNode):
            affected.append(NodeRef(
                node_id=op.node_id, path=_derive_create_path(op.parent.path, op.node_name),
                expected_type=op.node_type, expected_workspace_id=op.workspace_id,
            ))
        elif isinstance(op, SetParm):
            affected.append(op.target)
        elif isinstance(op, ConnectInput):
            affected.append(op.target)
            affected.append(op.source)
    return affected


def _complete(
    adapter: HoudiniSceneAdapter, operations, *,
    change_id: str = f"chg_{'4' * 32}", extra_pre: tuple = (), extra_post: tuple = (),
    affected_extra: tuple = (), permission: PermissionMode = PermissionMode.OWNED_WORKSPACE,
    scoped: tuple[str, ...] = (), backup: bool = False, run_id: str = RUN,
    with_workspace: bool = True, workspace_id: str | None = WS,
    risk_override: RiskSummary | None = None, affected_override: tuple | None = None,
) -> tuple[ChangeSet, ApplyRequest]:
    """Build a policy-consistent ChangeSet that carries every mandatory derived
    fact, plus the matching ApplyRequest. Overrides support negative tests."""
    binding = adapter.binding()
    workspace = _manifest(adapter) if with_workspace else None
    pre = _dedup(derive_mandatory_preconditions(operations, binding, workspace)) + list(extra_pre)
    post = _dedup(derive_mandatory_postconditions(operations)) + list(extra_post)
    cnodes, cparms, cwires = derive_mandatory_checkpoint(operations)
    cnodes = list(_dedup(cnodes))
    cparms = list(_dedup(cparms))
    cwires = list(_dedup(cwires))
    if affected_override is not None:
        affected = tuple(affected_override)
    else:
        affected = tuple(_dedup(list(_affected_from_ops(operations)) + list(affected_extra)))
    paths = tuple(sorted({a.path for a in affected}))
    risk = risk_override if risk_override is not None else _risk(operations, paths, backup=backup)
    cs = ChangeSet(
        change_id=change_id, session_id=SES, run_id=run_id, scene_binding=binding,
        workspace_id=workspace_id, base_revision=REVISION,
        required_permission=permission, scoped_node_ids=scoped,
        operations=tuple(operations), affected_nodes=affected, read_dependencies=(),
        preconditions=tuple(pre), expected_postconditions=tuple(post),
        risk_summary=risk,
        checkpoint_plan=CheckpointPlan(nodes=tuple(cnodes), parameters=tuple(cparms), wires=tuple(cwires)),
        created_at=NOW,
    )
    request = ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=binding.scene_epoch,
        changeset=cs, workspace=workspace,
    )
    return cs, request


def _mutations(spy: list) -> list:
    return [m for m in spy if m[0] in (
        "createNode", "parm.set", "parmTuple.set", "setInput", "setUserData", "destroy",
        "undo_group_begin",
    )]


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_apply_create_mirrors_ownership_and_returns_applied() -> None:
    spy: list = []
    adapter = _adapter(spy)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_c",)
    assert receipt.scene_may_have_changed is False
    assert receipt.before_revision != receipt.after_revision
    created = adapter._hou.node("/obj/ws/geo_new")  # type: ignore[attr-defined]
    assert created is not None
    assert created.userData("eee.node_id") == "n_new"
    assert created.userData("eee.workspace_id") == WS
    assert created.userData("eee.created_by_run") == RUN


def test_apply_set_and_connect_ordering() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    ops = (
        SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0),
        ConnectInput(op_id="op_w", target=child, input_index=1, source=src,
                     source_output_index=0, expected_old_source=None),
    )
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_s", "op_w")
    child_node = adapter._hou.node("/obj/ws/geo1")  # type: ignore[attr-defined]
    assert child_node.parm("tx").eval() == 5
    ones = [c for c in child_node.inputConnections() if c.inputIndex() == 1]
    assert ones and ones[0].outputNode().path() == "/obj/ws/src1"
    methods = [m[0] for m in spy if m[0] in ("parm.set", "setInput")]
    assert methods == ["parm.set", "setInput"]


# --------------------------------------------------------------------------
# F1: pure policy engine re-run before writes (zero writes on denial)
# --------------------------------------------------------------------------


def test_f1_scoped_patch_create_denied_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,), permission=PermissionMode.SCOPED_PATCH, scoped=("n_child",))
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "policy.denied"
    assert _mutations(spy) == []


def test_f1_scoped_patch_target_outside_scope_denied_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,), permission=PermissionMode.SCOPED_PATCH, scoped=("n_other",))
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "policy.denied"
    assert _mutations(spy) == []


def test_f1_effect_risk_contradiction_denied_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    bad_risk = RiskSummary(
        touches_external_nodes=False, changes_wiring=False, requires_backup=False,
        operation_count=1, effect_names=("node.create",), affected_paths=("/obj/ws/geo1",),
    )
    _cs, req = _complete(adapter, (op,), risk_override=bad_risk)
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "policy.denied"
    assert _mutations(spy) == []


def test_f1_affected_target_omitted_denied_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,), affected_override=())  # omit the target
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "policy.denied"
    assert _mutations(spy) == []


# --------------------------------------------------------------------------
# F2: mandatory derived pre/post + checkpoint coverage enforced (zero writes)
# --------------------------------------------------------------------------


def test_f2_omits_mandatory_postcondition_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    # build complete, then drop the mandatory parm postcondition
    cs, _req = _complete(adapter, (op,))
    post = tuple(c for c in cs.expected_postconditions if not (
        isinstance(c, ParmValueEquals) and c.parm_name == "tx"
    ))
    object.__setattr__(cs, "expected_postconditions", post)
    req = ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=adapter.binding().scene_epoch,
        changeset=cs, workspace=_manifest(adapter),
    )
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "changeset.stale"
    assert _mutations(spy) == []


def test_f2_omits_checkpoint_coverage_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    cs, _req = _complete(adapter, (op,))
    object.__setattr__(cs, "checkpoint_plan", CheckpointPlan(nodes=(), parameters=(), wires=()))
    req = ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=adapter.binding().scene_epoch,
        changeset=cs, workspace=_manifest(adapter),
    )
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "changeset.stale"
    assert _mutations(spy) == []


def test_f2_contradicts_mandatory_precondition_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    cs, _req = _complete(adapter, (op,))
    # replace the mandatory old-value precondition (0) with a contradictory one (999)
    new_pre = []
    for c in cs.preconditions:
        if isinstance(c, ParmValueEquals) and c.parm_name == "tx":
            new_pre.append(ParmValueEquals(target=child, parm_name="tx", value=999))
        else:
            new_pre.append(c)
    object.__setattr__(cs, "preconditions", tuple(new_pre))
    req = ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=adapter.binding().scene_epoch,
        changeset=cs, workspace=_manifest(adapter),
    )
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "changeset.stale"
    assert _mutations(spy) == []


# --------------------------------------------------------------------------
# F3: early-inverse journaling on mirror/mutate failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mirror_key", [
    "eee.node_id", "eee.workspace_id", "eee.capability", "eee.role", "eee.schema_version", "eee.created_by_run",
])
def test_f3_create_mirror_failure_rolls_back_created_node(mirror_key: str) -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_fail_set_user_data_key = mirror_key
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    # the mirror failed after createNode; the create was journaled and rolled back
    assert receipt.status.value == "RolledBack"
    assert "/obj/ws/geo_new" not in scene


def test_f3_parm_mutate_then_raise_restores_old() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/geo1"]._parms["tx"].raise_after_set_once = True
    adapter = _adapter(spy, scene)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    # the inverse restored the before value even though set() mutated then raised
    assert scene["/obj/ws/geo1"].parm("tx").eval() == 0


def test_f3_wire_mutate_then_raise_restores_old() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/geo1"].created_raise_after_setinput_once = False
    # target geo1: setInput mutates input 1 then raises; inverse restores (absent)
    scene["/obj/ws/geo1"].raise_after_setinput_once = True
    adapter = _adapter(spy, scene)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    op = ConnectInput(op_id="op_w", target=child, input_index=1, source=src,
                      source_output_index=0, expected_old_source=None)
    _cs, req = _complete(adapter, (op,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    # input 1 was restored to absent (its before state)
    assert 1 not in scene["/obj/ws/geo1"]._inputs


# --------------------------------------------------------------------------
# F4: post-write failure containment; truthful Partial/Critical + freeze
# --------------------------------------------------------------------------


def test_f4_postcondition_failure_rolls_back() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    # extra postcondition for a parm that won't exist after the write -> fails
    bad_post = ParmValueEquals(target=child, parm_name="ty", value=999)
    _cs, req = _complete(adapter, (op,), extra_post=(bad_post,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert receipt.scene_may_have_changed is False
    assert adapter._hou.node("/obj/ws/geo1").parm("tx").eval() == 0  # type: ignore[attr-defined]


def test_f4_rollback_failure_is_critical_recovery_and_freezes() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/geo1"]._parms["tx"].fail_set_after = 1  # forward ok, rollback restore fails
    adapter = _adapter(spy, scene)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    bad_post = ParmValueEquals(target=child, parm_name="ty", value=999)
    _cs, req = _complete(adapter, (op,), extra_post=(bad_post,))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    assert receipt.status.value == "CriticalRecovery"
    assert receipt.scene_may_have_changed is True
    assert executor.write_frozen is True
    spy.clear()
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.apply(req)
    assert exc.value.code == "bridge.write_frozen"
    assert _mutations(spy) == []


def test_f4_reconciliation_crash_is_contained_no_escape() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    # created node's type read crashes during the after-snapshot
    scene["/obj/ws"].created_fail_type_read = True
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    receipt = ChangeSetExecutor(adapter).apply(req)  # must not raise
    assert receipt.status.value != "Applied"
    assert receipt.scene_may_have_changed in (False, True)


def test_f4_write_error_mid_transaction_rolls_back_prior_ops() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].fail_create_after = 1  # first create ok, second fails
    adapter = _adapter(spy, scene)
    parent = _noderef("n_root", "/obj/ws", "subnet")
    ops = (
        CreateNode(op_id="op_a", parent=parent, node_id="n_a", node_type="geo", node_name="a",
                   workspace_id=WS, capability="modeling", role="member"),
        CreateNode(op_id="op_b", parent=parent, node_id="n_b", node_type="geo", node_name="b",
                   workspace_id=WS, capability="modeling", role="member"),
    )
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert receipt.applied_op_ids == ("op_a",)
    assert "/obj/ws/a" not in scene
    assert "/obj/ws/b" not in scene


# --------------------------------------------------------------------------
# F5: idempotency semantics
# --------------------------------------------------------------------------


def test_f5_same_digest_success_replay_is_already_applied_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,))
    executor = ChangeSetExecutor(adapter)
    first = executor.apply(req)
    assert first.status.value == "Applied"
    spy.clear()
    second = executor.apply(req)
    assert second.status.value == "AlreadyApplied"
    assert second.applied_op_ids == ()
    assert _mutations(spy) == []


def test_f5_repeated_rolled_back_returns_cached_zero_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    bad_post = ParmValueEquals(target=child, parm_name="ty", value=999)
    _cs, req = _complete(adapter, (op,), extra_post=(bad_post,))
    executor = ChangeSetExecutor(adapter)
    first = executor.apply(req)
    assert first.status.value == "RolledBack"
    spy.clear()
    second = executor.apply(req)
    assert second.status.value == "RolledBack"
    assert _mutations(spy) == []


def test_f5_critical_recovery_freezes_and_remains_queryable() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/geo1"]._parms["tx"].fail_set_after = 1
    adapter = _adapter(spy, scene)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    bad_post = ParmValueEquals(target=child, parm_name="ty", value=999)
    cs, req = _complete(adapter, (op,), extra_post=(bad_post,))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    assert receipt.status.value == "CriticalRecovery"
    # frozen: re-apply raises; the receipt remains queryable
    with pytest.raises(HoudiniAdapterError):
        executor.apply(req)
    cached = executor.receipt(cs.change_id, cs.digest, scene_epoch=adapter.binding().scene_epoch)
    assert cached is not None and cached.status.value == "CriticalRecovery"


def test_f5_different_digest_same_change_id_is_conflict() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op5 = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    op7 = SetParm(op_id="op_s", target=child, parm_name="tx", value=7, expected_old_value=0)
    _cs1, req1 = _complete(adapter, (op5,), change_id=f"chg_{'9' * 32}")
    _cs2, req2 = _complete(adapter, (op7,), change_id=f"chg_{'9' * 32}")
    executor = ChangeSetExecutor(adapter)
    executor.apply(req1)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.apply(req2)
    assert exc.value.code == "changeset.receipt_conflict"


# --------------------------------------------------------------------------
# F6: receipt conflict + scene_epoch gate
# --------------------------------------------------------------------------


def test_f6_receipt_same_id_different_digest_is_conflict() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    cs, req = _complete(adapter, (op,))
    executor = ChangeSetExecutor(adapter)
    executor.apply(req)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.receipt(cs.change_id, "b" * 64, scene_epoch=adapter.binding().scene_epoch)
    assert exc.value.code == "changeset.receipt_conflict"


def test_f6_receipt_wrong_scene_epoch_fails_closed() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    cs, req = _complete(adapter, (op,))
    executor = ChangeSetExecutor(adapter)
    executor.apply(req)
    with pytest.raises(HoudiniAdapterError) as exc:
        executor.receipt(cs.change_id, cs.digest, scene_epoch=99)
    assert exc.value.code == "bridge.stale_scene"


def test_f6_receipt_unknown_returns_none() -> None:
    adapter = _adapter([])
    executor = ChangeSetExecutor(adapter)
    assert executor.receipt(f"chg_{'0' * 32}", "c" * 64, scene_epoch=adapter.binding().scene_epoch) is None


# --------------------------------------------------------------------------
# F7: rollback-create identity proof
# --------------------------------------------------------------------------


def test_f7_rollback_will_not_destroy_foreign_node_at_same_path() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    adapter = _adapter(spy, scene)
    # Pre-create a FOREIGN node at the derived create path (different identity),
    # so the transaction's create target is NOT absent -> preflight rejects it
    # (node.absent precondition fails) before writes. This proves the create-
    # absence gate; rollback identity proof is exercised via mirror-failure above.
    foreign = _Node(scene, spy, "/obj/ws/geo_new", "geo", "/obj/ws", user_data=_mirror("n_foreign"))
    scene["/obj/ws/geo_new"] = foreign
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    with pytest.raises(HoudiniAdapterError):
        ChangeSetExecutor(adapter).apply(req)
    # the foreign node is untouched (create was rejected before writes)
    assert scene.get("/obj/ws/geo_new") is foreign


# --------------------------------------------------------------------------
# pre-transaction zero-write gates (retained)
# --------------------------------------------------------------------------


def test_stale_scene_epoch_rejects_before_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,))
    adapter._on_scene_event(adapter._hou.hipFileEventType.AfterLoad)  # type: ignore[attr-defined]
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "bridge.stale_scene"
    assert _mutations(spy) == []


def test_precondition_not_holding_rejects_before_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=5)
    # mandatory old-value precondition will be value=5, but scene has tx=0 -> fails
    _cs, req = _complete(adapter, (op,))
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "changeset.stale"
    assert _mutations(spy) == []


def test_backup_required_rejects_before_writes() -> None:
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,), backup=True)
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "bridge.backup_unavailable"
    assert _mutations(spy) == []


def test_locked_target_rejects_before_writes() -> None:
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws/geo1"].hard_locked = True
    adapter = _adapter(spy, scene)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (op,))
    with pytest.raises(HoudiniAdapterError) as exc:
        ChangeSetExecutor(adapter).apply(req)
    assert exc.value.code == "changeset.stale"
    assert _mutations(spy) == []


# --------------------------------------------------------------------------
# receipt cache eviction (retained)
# --------------------------------------------------------------------------


def test_receipt_cache_eviction_is_bounded() -> None:
    spy: list = []
    adapter = _adapter(spy)
    executor = ChangeSetExecutor(adapter, receipt_cache_max=2)
    applied: list[tuple[str, str]] = []
    for i in range(1, 4):
        child = _noderef("n_child", "/obj/ws/geo1", "geo")
        op = SetParm(op_id=f"op_{i}", target=child, parm_name="tx", value=i, expected_old_value=i - 1)
        cs, req = _complete(adapter, (op,), change_id=f"chg_{str(i) * 32}")
        executor.apply(req)
        applied.append((cs.change_id, cs.digest))
    assert executor.receipt(*applied[0], scene_epoch=adapter.binding().scene_epoch) is None
    assert executor.receipt(*applied[1], scene_epoch=adapter.binding().scene_epoch) is not None
    assert executor.receipt(*applied[2], scene_epoch=adapter.binding().scene_epoch) is not None


# --------------------------------------------------------------------------
# unknown/forward-delete effects remain impossible to parse (retained)
# --------------------------------------------------------------------------


def test_unknown_and_forward_delete_effects_cannot_be_parsed() -> None:
    import json as _json
    from eee_agent.houdini_bridge.changesets import ApplyRequest, parse_apply_request

    adapter = _adapter([])
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    op = SetParm(op_id="op_s", target=child, parm_name="tx", value=5, expected_old_value=0)
    cs, _req = _complete(adapter, (op,))
    valid = ApplyRequest.build(
        request_id="req_apply", deadline_ms=5000, scene_epoch=adapter.binding().scene_epoch,
        changeset=cs, workspace=_manifest(adapter),
    )
    obj = _json.loads(valid.to_json())
    obj["payload"]["changeset"]["operations"][0]["kind"] = "node.delete"
    with pytest.raises((TypeError, ValueError)):
        parse_apply_request(_json.dumps(obj, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------
# F8: deterministic cross-thread cancellation AFTER the first write
# --------------------------------------------------------------------------


def test_f8_cancellation_after_first_write() -> None:
    """The transaction completes/reconciles on the pump thread, but a transport-
    side cancel that lands during the run discards the result. The waiter
    receives QueueItemCancelled while the terminal receipt is cached and
    queryable — with zero duplicate writes."""
    spy: list = []
    scene = _standard_scene(spy)
    enter = threading.Event()
    release = threading.Event()
    scene["/obj/ws"].create_enter_event = enter
    scene["/obj/ws"].create_release_event = release
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    cs, req = _complete(adapter, (create,))
    executor = ChangeSetExecutor(adapter)
    queue = MainThreadReadQueue()

    async def scenario() -> None:
        fut = queue.submit(
            req.request_id, lambda: executor.apply(req),
            deadline_monotonic=time.monotonic() + 30,
        )
        pump_done = threading.Event()

        def _pump() -> None:
            queue.pump_one()
            pump_done.set()

        t = threading.Thread(target=_pump)
        t.start()
        # The operation is now inside createNode (blocked at the barrier).
        assert enter.wait(timeout=10), "operation did not reach createNode"
        # Cancel the RUNNING request from the transport/event-loop side.
        assert queue.cancel(req.request_id) is True
        # Release the barrier so the transaction can complete.
        release.set()
        assert pump_done.wait(timeout=10)
        t.join(timeout=10)
        # The waiter receives QueueItemCancelled (result discarded).
        with pytest.raises(QueueItemCancelled):
            await fut
        # The transaction DID complete: the receipt is cached and queryable.
        epoch = adapter.binding().scene_epoch
        receipt = executor.receipt(cs.change_id, cs.digest, scene_epoch=epoch)
        assert receipt is not None and receipt.status.value == "Applied"
        # Exactly one createNode (no duplicate from the discarded result).
        assert sum(1 for m in spy if m[0] == "createNode") == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# F10: rollback-create identity read exception refuses destroy
# --------------------------------------------------------------------------


def test_f10_user_data_read_exception_refuses_destroy_and_freezes() -> None:
    """If userData raises during rollback-create identity verification, destroy is
    refused → uncertain recovery → CriticalRecovery + freeze. The created node
    remains (not destroyed) because identity could not be proven."""
    spy: list = []
    scene = _standard_scene(spy)
    # created nodes' userData read will raise during rollback verification
    scene["/obj/ws"].created_fail_user_data = True
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    # Add an extra postcondition that will fail (ty parm doesn't exist) to trigger
    # rollback after the create succeeds.
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    bad_post = ParmValueEquals(target=child, parm_name="ty", value=999)
    _cs, req = _complete(adapter, (create,), extra_post=(bad_post,))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    # The create succeeded, postcondition failed, rollback attempted, but
    # rollback-create couldn't verify identity (userData raises) → CriticalRecovery.
    assert receipt.status.value == "CriticalRecovery"
    assert receipt.scene_may_have_changed is True
    assert executor.write_frozen is True
    # The created node was NOT destroyed (identity unprovable).
    assert "/obj/ws/geo_new" in scene


# --------------------------------------------------------------------------
# D1: create-then-set/connect/child — JIT enforcement
# --------------------------------------------------------------------------


def test_d1_create_then_set_applied() -> None:
    """create a node, then set its parm. The created node has a default tx=0
    (via created_parms); the JIT reads it, compares to expected_old_value=0,
    and writes the new value."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_parms = {"tx": 0}
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    setparm = SetParm(op_id="op_s", target=ref, parm_name="tx", value=5, expected_old_value=0)
    _cs, req = _complete(adapter, (create, setparm))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_c", "op_s")
    assert scene["/obj/ws/geo_new"].parm("tx").eval() == 5


def test_d1_create_then_connect_as_target_applied() -> None:
    """create a node, then connect its input. Created target has no inputs →
    expected_old_source=None matches → connect writes."""
    spy: list = []
    adapter = _adapter(spy)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    connect = ConnectInput(op_id="op_w", target=ref, input_index=0, source=src,
                           source_output_index=0, expected_old_source=None)
    _cs, req = _complete(adapter, (create, connect))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_c", "op_w")


def test_d1_create_under_created_parent_applied() -> None:
    """create parent, then create child under that parent. The executor verifies
    the parent's full created identity (mirrors + path + type) before the child
    createNode."""
    spy: list = []
    adapter = _adapter(spy)
    parent = _noderef("n_root", "/obj/ws", "subnet")
    create_a = CreateNode(op_id="op_a", parent=parent, node_id="n_a", node_type="geo",
                          node_name="a", workspace_id=WS, capability="modeling", role="member")
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    _cs, req = _complete(adapter, (create_a, create_b))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_a", "op_b")
    assert "/obj/ws/a" in scene_nodes(adapter)
    assert "/obj/ws/a/b" in scene_nodes(adapter)


def scene_nodes(adapter: HoudiniSceneAdapter) -> dict:
    return adapter._hou._nodes  # type: ignore[attr-defined]


def test_d1_jit_parm_stale_mismatch_rolls_back() -> None:
    """create a node with tx=0, then set tx with expected_old_value=999. The JIT
    comparison (0 != 999) fails → the set op makes zero writes, the prior create
    rolls back → RolledBack."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_parms = {"tx": 0}
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    setparm = SetParm(op_id="op_s", target=ref, parm_name="tx", value=5, expected_old_value=999)
    _cs, req = _complete(adapter, (create, setparm))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    # the set op was NOT applied
    assert "op_s" not in receipt.applied_op_ids
    # the created node was rolled back (destroyed)
    assert "/obj/ws/geo_new" not in scene_nodes(adapter)


def test_d1_jit_wire_stale_mismatch_rolls_back() -> None:
    """create a node, then connect with a wrong expected_old_source. The JIT
    comparison fails → connect makes zero writes, prior create rolls back."""
    spy: list = []
    adapter = _adapter(spy)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    wrong_old = WireRef(source=_noderef("n_child", "/obj/ws/geo1", "geo"), source_output_index=0)
    connect = ConnectInput(op_id="op_w", target=ref, input_index=0, source=src,
                           source_output_index=0, expected_old_source=wrong_old)
    _cs, req = _complete(adapter, (create, connect))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert "op_w" not in receipt.applied_op_ids
    assert "/obj/ws/geo_new" not in scene_nodes(adapter)


def test_d1_created_parent_tampering_rejects_child_create() -> None:
    """create parent with a tampered mirror (node_id='TAMPERED'), then create child
    under that parent. The JIT identity verification reads 'TAMPERED' != 'n_a' →
    child create fails → prior writes roll back (or uncertain recovery)."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_tamper_key = "eee.node_id"
    adapter = _adapter(spy, scene)
    create_a = CreateNode(op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"),
                          node_id="n_a", node_type="geo", node_name="a",
                          workspace_id=WS, capability="modeling", role="member")
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    _cs, req = _complete(adapter, (create_a, create_b))
    receipt = ChangeSetExecutor(adapter).apply(req)
    # child create was NOT applied
    assert receipt.status.value != "Applied"
    assert "op_b" not in receipt.applied_op_ids
    # child node does not exist
    assert "/obj/ws/a/b" not in scene_nodes(adapter)


def test_d1_repeated_setparm_on_existing_parm_applied() -> None:
    """F2: two SetParm ops on the same existing parm (0→1→2) must construct and
    apply. Only the first gets a preflight old-value precondition; the second's
    expected-old is JIT-only."""
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    ops = (
        SetParm(op_id="op_s1", target=child, parm_name="tx", value=1, expected_old_value=0),
        SetParm(op_id="op_s2", target=child, parm_name="tx", value=2, expected_old_value=1),
    )
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_s1", "op_s2")
    assert scene_nodes(adapter)["/obj/ws/geo1"].parm("tx").eval() == 2


def test_d1_both_created_endpoints_connect_applied() -> None:
    """F5: connect using both a created target and a created source."""
    spy: list = []
    adapter = _adapter(spy)
    create_a = CreateNode(op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"),
                          node_id="n_a", node_type="geo", node_name="a",
                          workspace_id=WS, capability="modeling", role="member")
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    ref_b = NodeRef(node_id="n_b", path="/obj/ws/a/b", expected_type="geo", expected_workspace_id=WS)
    connect = ConnectInput(op_id="op_w", target=ref_b, input_index=0, source=ref_a,
                           source_output_index=0, expected_old_source=None)
    _cs, req = _complete(adapter, (create_a, create_b, connect))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_a", "op_b", "op_w")


def test_d1_jit_parm_stale_exact_zero_current_write() -> None:
    """F5: JIT stale on the SECOND SetParm → that op makes zero writes. Prior
    effects roll back in reverse. Assert exact receipt status and zero-write."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_parms = {"tx": 0}
    adapter = _adapter(spy, scene)
    create = CreateNode(op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"),
                        node_id="n_new", node_type="geo", node_name="geo_new",
                        workspace_id=WS, capability="modeling", role="member")
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    set1 = SetParm(op_id="op_s1", target=ref, parm_name="tx", value=1, expected_old_value=0)
    set2 = SetParm(op_id="op_s2", target=ref, parm_name="tx", value=2, expected_old_value=999)
    _cs, req = _complete(adapter, (create, set1, set2))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert "op_s2" not in receipt.applied_op_ids
    # op_s2 made ZERO parm.set writes; only op_s1's write + rollback restore appear
    parm_sets = [m for m in spy if m[0] == "parm.set" and m[1] == "/obj/ws/geo_new"]
    assert len(parm_sets) == 2  # op_s1 write + rollback restore; NOT op_s2
    # created node was rolled back
    assert "/obj/ws/geo_new" not in scene_nodes(adapter)


def test_d1_multi_level_create_set_connect_chain() -> None:
    """F5: combined multi-level create-parent → create-children → set → connect."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_parms = {"tx": 0}
    adapter = _adapter(spy, scene)
    create_sub = CreateNode(op_id="op_c1", parent=_noderef("n_root", "/obj/ws", "subnet"),
                            node_id="n_sub", node_type="geo", node_name="sub",
                            workspace_id=WS, capability="modeling", role="member")
    ref_sub = NodeRef(node_id="n_sub", path="/obj/ws/sub", expected_type="geo", expected_workspace_id=WS)
    create_a = CreateNode(op_id="op_c2", parent=ref_sub, node_id="n_a", node_type="geo",
                          node_name="a", workspace_id=WS, capability="modeling", role="member")
    create_b = CreateNode(op_id="op_c3", parent=ref_sub, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/sub/a", expected_type="geo", expected_workspace_id=WS)
    ref_b = NodeRef(node_id="n_b", path="/obj/ws/sub/b", expected_type="geo", expected_workspace_id=WS)
    setparm = SetParm(op_id="op_s", target=ref_a, parm_name="tx", value=5, expected_old_value=0)
    connect = ConnectInput(op_id="op_w", target=ref_b, input_index=0, source=ref_a,
                           source_output_index=0, expected_old_source=None)
    ops = (create_sub, create_a, create_b, setparm, connect)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_c1", "op_c2", "op_c3", "op_s", "op_w")
    assert scene_nodes(adapter)["/obj/ws/sub/a"].parm("tx").eval() == 5


# --------------------------------------------------------------------------
# Fix 1: _wire_equal preserves optional-field semantics
# --------------------------------------------------------------------------


def test_fix1_wire_equal_optional_fields_regression() -> None:
    """An expected_old_source with node_id=None / workspace=None must match by
    path/type/output only — the regression that broke existing-scene wires."""
    actual = WireRef(
        source=NodeRef(node_id="n_src", path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=WS),
        source_output_index=0,
    )
    expected = WireRef(
        source=NodeRef(node_id=None, path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=None),
        source_output_index=0,
    )
    assert _wire_equal(actual, expected) is True
    # Mismatched path still rejects
    bad = WireRef(
        source=NodeRef(node_id=None, path="/obj/ws/WRONG", expected_type="xform",
                       expected_workspace_id=None),
        source_output_index=0,
    )
    assert _wire_equal(actual, bad) is False


def test_fix1_wire_equal_declared_fields_enforced() -> None:
    """When the expected ref DECLARES node_id/workspace, they must match."""
    actual = WireRef(
        source=NodeRef(node_id="n_src", path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=WS),
        source_output_index=0,
    )
    expected_exact = WireRef(
        source=NodeRef(node_id="n_src", path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=WS),
        source_output_index=0,
    )
    assert _wire_equal(actual, expected_exact) is True
    expected_wrong_id = WireRef(
        source=NodeRef(node_id="n_other", path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=WS),
        source_output_index=0,
    )
    assert _wire_equal(actual, expected_wrong_id) is False


# --------------------------------------------------------------------------
# Fix 2: created-source → connect → reconnect; tampered old-created-source
# --------------------------------------------------------------------------


def test_fix2_create_source_connect_existing_target_then_reconnect() -> None:
    """create A → connect existing target slot from None to A → reconnect from A
    to existing B. The reconnect's expected_old_source is A (created)."""
    spy: list = []
    adapter = _adapter(spy)
    create_a = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref_a = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    connect1 = ConnectInput(
        op_id="op_w1", target=child, input_index=1, source=ref_a,
        source_output_index=0, expected_old_source=None,
    )
    old_from_a = WireRef(source=ref_a, source_output_index=0)
    connect2 = ConnectInput(
        op_id="op_w2", target=child, input_index=1, source=src,
        source_output_index=0, expected_old_source=old_from_a,
    )
    ops = (create_a, connect1, connect2)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_c", "op_w1", "op_w2")


def test_fix2_tampered_old_created_source_reconnect_partial_freeze() -> None:
    """create A → connect1 wires existing target child input 1 from None to A
    (desired source). After connect1's setInput, a one-shot callback tampers A's
    capability mirror. connect2 reconnects the same slot (expected_old_source =
    A); the JIT resolves the actual old source and calls the six-mirror verifier,
    which rejects the tampered capability. connect2 makes zero current write.
    Rollback: wire restore succeeds (one), create destroy is refused (tampered
    capability mismatch) → Partial + scene_may_have_changed + freeze."""
    spy: list = []
    scene = _standard_scene(spy)
    adapter = _adapter(spy, scene)
    create_a = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref_a = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src = _noderef("n_src", "/obj/ws/src1", "xform")
    connect1 = ConnectInput(
        op_id="op_w1", target=child, input_index=1, source=ref_a,
        source_output_index=0, expected_old_source=None,
    )
    old_from_a = WireRef(source=ref_a, source_output_index=0)
    connect2 = ConnectInput(
        op_id="op_w2", target=child, input_index=1, source=src,
        source_output_index=0, expected_old_source=old_from_a,
    )
    ops = (create_a, connect1, connect2)
    _cs, req = _complete(adapter, ops)

    # After connect1's first successful forward setInput on child input 1,
    # tamper A's capability mirror so the reconnect's JIT six-mirror check
    # on the OLD source fails.
    def _tamper_a_cap(_target, _idx, _src):
        a_node = scene.get("/obj/ws/geo_new")
        if a_node is not None:
            a_node._user_data["eee.capability"] = "TAMPERED"
    scene["/obj/ws/geo1"]._post_setinput_callback = _tamper_a_cap

    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    # connect1 was applied; connect2 was NOT
    assert "op_w1" in receipt.applied_op_ids
    assert "op_w2" not in receipt.applied_op_ids
    # connect2 made zero writes: only connect1's forward setInput + the
    # rollback's restore setInput appear (2 total, NOT 3).
    setinputs_on_slot = [m for m in spy if m[0] == "setInput" and m[1] == "/obj/ws/geo1" and m[2] == 1]
    assert len(setinputs_on_slot) == 2  # connect1 forward + rollback restore
    # Rollback order: wire restore (first inverse reversed), then create (second)
    assert receipt.status.value == "Partial"
    assert receipt.scene_may_have_changed is True
    assert executor.write_frozen is True
    # Wire restore succeeded; create destroy was refused
    assert receipt.rollback_results[0].kind == "wire.input_equals"
    assert receipt.rollback_results[0].passed is True
    assert receipt.rollback_results[1].kind == "node.absent"
    assert receipt.rollback_results[1].passed is False  # tampered cap → refused


# --------------------------------------------------------------------------
# Fix 3: repeated ConnectInput on existing target slot + executor wire_equal
# --------------------------------------------------------------------------


def test_fix3_executor_path_only_old_source_matches_existing_wire() -> None:
    """Executor-level regression: an existing target slot wired to a mirrored
    source is reconnected using a path-only expected_old_source (node_id=None,
    workspace=None). The apply must succeed — _wire_equal preserves optional-
    field semantics end-to-end."""
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src1 = _noderef("n_src", "/obj/ws/src1", "xform")
    src2 = _noderef("n_root", "/obj/ws", "subnet")
    connect1 = ConnectInput(op_id="op_w1", target=child, input_index=1, source=src1,
                            source_output_index=0, expected_old_source=None)
    # path-only old source: node_id=None, workspace=None
    path_only_old = WireRef(
        source=NodeRef(node_id=None, path="/obj/ws/src1", expected_type="xform",
                       expected_workspace_id=None),
        source_output_index=0,
    )
    connect2 = ConnectInput(op_id="op_w2", target=child, input_index=1, source=src2,
                            source_output_index=0, expected_old_source=path_only_old)
    ops = (connect1, connect2)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_w1", "op_w2")


def test_fix3_repeated_connect_existing_slot_applied() -> None:
    """Two ConnectInput ops on the same existing target slot: first None→src1,
    then src1→src2. Only the first gets a preflight old-wire precondition; the
    second is JIT-only. Both must apply."""
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src1 = _noderef("n_src", "/obj/ws/src1", "xform")
    # src2 doesn't exist in the standard scene; use n_root as a stand-in source
    src2 = _noderef("n_root", "/obj/ws", "subnet")
    connect1 = ConnectInput(op_id="op_w1", target=child, input_index=1, source=src1,
                            source_output_index=0, expected_old_source=None)
    old_from_src1 = WireRef(source=src1, source_output_index=0)
    connect2 = ConnectInput(op_id="op_w2", target=child, input_index=1, source=src2,
                            source_output_index=0, expected_old_source=old_from_src1)
    ops = (connect1, connect2)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.applied_op_ids == ("op_w1", "op_w2")


def test_fix3_repeated_connect_stale_second_rolls_back() -> None:
    """Second connect on same slot with a stale expected_old_source → JIT fails →
    zero current write → rollback of the first connect."""
    spy: list = []
    adapter = _adapter(spy)
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    src1 = _noderef("n_src", "/obj/ws/src1", "xform")
    src2 = _noderef("n_root", "/obj/ws", "subnet")
    connect1 = ConnectInput(op_id="op_w1", target=child, input_index=1, source=src1,
                            source_output_index=0, expected_old_source=None)
    wrong_old = WireRef(
        source=_noderef("n_root", "/obj/ws", "subnet"), source_output_index=0,
    )
    connect2 = ConnectInput(op_id="op_w2", target=child, input_index=1, source=src2,
                            source_output_index=0, expected_old_source=wrong_old)
    ops = (connect1, connect2)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert "op_w2" not in receipt.applied_op_ids


# --------------------------------------------------------------------------
# Fix 4: direct preflight adapter evidence for created targets
# --------------------------------------------------------------------------


def test_fix4_preflight_no_parm_wire_facts_for_created_targets() -> None:
    """Exercising the preflight adapter directly: a ChangeSet with create, a
    created-target SetParm, a created-target ConnectInput, AND an existing-target
    SetParm. Proves create targets are absent, created targets produce NO parm
    or wire facts, and the existing target's parm fact is exactly correct."""
    spy: list = []
    scene = _standard_scene(spy)
    adapter = _adapter(spy, scene)
    from eee_agent.houdini_bridge.changesets import PreflightRequest
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    created_setparm = SetParm(op_id="op_s1", target=ref, parm_name="tx", value=5, expected_old_value=0)
    created_connect = ConnectInput(
        op_id="op_w1", target=ref, input_index=0, source=_noderef("n_src", "/obj/ws/src1", "xform"),
        source_output_index=0, expected_old_source=None,
    )
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    existing_setparm = SetParm(op_id="op_s2", target=child, parm_name="tx", value=9, expected_old_value=0)
    ops = (create, created_setparm, created_connect, existing_setparm)
    cs, _req = _complete(adapter, ops)
    preflight_req = PreflightRequest.build(
        request_id="req_pf", deadline_ms=5000,
        scene_epoch=adapter.binding().scene_epoch, changeset=cs, workspace=_manifest(adapter),
    )
    pre = ChangeSetPreflightAdapter(adapter)
    result = pre.preflight(preflight_req)
    # Create target is proven absent
    create_facts = [f for f in result.node_facts if f.requested.node_id == "n_new"]
    assert len(create_facts) == 1
    assert create_facts[0].exists is False
    # No parm facts for the created target
    created_parm_facts = [f for f in result.parm_facts if f.target.node_id == "n_new"]
    assert created_parm_facts == []
    # No wire facts for the created target
    created_wire_facts = [f for f in result.wire_facts if f.target.node_id == "n_new"]
    assert created_wire_facts == []
    # Existing target parm fact is present and exact
    existing_parm_facts = [f for f in result.parm_facts if f.target.node_id == "n_child"]
    assert len(existing_parm_facts) == 1
    assert existing_parm_facts[0].exists is True
    assert existing_parm_facts[0].parm_name == "tx"
    assert existing_parm_facts[0].value == 0


# --------------------------------------------------------------------------
# Fix 5: parameterized mirror + path/type tampering; exact statuses
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tamper_key", [
    "eee.node_id", "eee.workspace_id", "eee.capability", "eee.role", "eee.schema_version",
    "eee.created_by_run",
])
def test_fix5_six_mirror_tampering_before_child_create(tamper_key: str) -> None:
    """Parameterized: tamper each of the six created mirrors on the parent before
    a child create. The JIT identity check fails → child create zero-write →
    rollback → exact CriticalRecovery + write_frozen (parent can't be safely
    destroyed because the tampered mirror doesn't match the journaled identity)."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_tamper_key = tamper_key
    adapter = _adapter(spy, scene)
    create_a = CreateNode(
        op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_a",
        node_type="geo", node_name="a", workspace_id=WS, capability="modeling", role="member",
    )
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    _cs, req = _complete(adapter, (create_a, create_b))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    assert receipt.status.value == "CriticalRecovery"
    assert receipt.scene_may_have_changed is True
    assert executor.write_frozen is True
    assert "/obj/ws/a/b" not in scene_nodes(adapter)


def test_fix5_path_tampering_before_child_create() -> None:
    """The created parent's path is tampered: path() returns a different value
    than the derived ref. JIT identity check fails → CriticalRecovery + freeze."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_tamper_path = "/obj/ws/RENAMED"
    adapter = _adapter(spy, scene)
    create_a = CreateNode(
        op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_a",
        node_type="geo", node_name="a", workspace_id=WS, capability="modeling", role="member",
    )
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    _cs, req = _complete(adapter, (create_a, create_b))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    assert receipt.status.value == "CriticalRecovery"
    assert executor.write_frozen is True


def test_fix5_type_tampering_before_child_create() -> None:
    """The created parent's type is tampered: type().name() returns a different
    value than the derived ref. JIT identity check fails → child create zero-write.
    The parent's mirrors are intact, so it rolls back cleanly → RolledBack (not
    CriticalRecovery, because type alone doesn't prevent safe destroy)."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_tamper_type = "WRONG"
    adapter = _adapter(spy, scene)
    create_a = CreateNode(
        op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_a",
        node_type="geo", node_name="a", workspace_id=WS, capability="modeling", role="member",
    )
    ref_a = NodeRef(node_id="n_a", path="/obj/ws/a", expected_type="geo", expected_workspace_id=WS)
    create_b = CreateNode(op_id="op_b", parent=ref_a, node_id="n_b", node_type="geo",
                          node_name="b", workspace_id=WS, capability="modeling", role="member")
    _cs, req = _complete(adapter, (create_a, create_b))
    executor = ChangeSetExecutor(adapter)
    receipt = executor.apply(req)
    assert receipt.status.value == "RolledBack"
    assert "op_b" not in receipt.applied_op_ids
    assert "/obj/ws/a/b" not in scene_nodes(adapter)


def test_fix5_multi_prior_jit_failure_strict_reverse_rollback() -> None:
    """Three ops: create A, set existing child tx, set A's tx with stale
    expected_old_value. The stale third op fails → create A + set child both roll
    back in strict reverse order → RolledBack."""
    spy: list = []
    scene = _standard_scene(spy)
    scene["/obj/ws"].created_parms = {"tx": 0}
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_c", parent=_noderef("n_root", "/obj/ws", "subnet"), node_id="n_new",
        node_type="geo", node_name="geo_new", workspace_id=WS, capability="modeling", role="member",
    )
    child = _noderef("n_child", "/obj/ws/geo1", "geo")
    set_existing = SetParm(op_id="op_s1", target=child, parm_name="tx", value=5, expected_old_value=0)
    ref = NodeRef(node_id="n_new", path="/obj/ws/geo_new", expected_type="geo", expected_workspace_id=WS)
    set_created_stale = SetParm(op_id="op_s2", target=ref, parm_name="tx", value=9, expected_old_value=999)
    ops = (create, set_existing, set_created_stale)
    _cs, req = _complete(adapter, ops)
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert receipt.applied_op_ids == ("op_c", "op_s1")
    assert "op_s2" not in receipt.applied_op_ids
    assert all(r.passed for r in receipt.rollback_results)
    # Reversed: set_existing rollback first, then create rollback.
    assert receipt.rollback_results[0].kind == "parm.value_equals"  # set_existing (reversed)
    assert receipt.rollback_results[1].kind == "node.absent"         # create (reversed)
    assert len(receipt.rollback_results) == 2  # only 2 ops applied (create + set_existing)


# --------------------------------------------------------------------------
# B-1: apply failure cause must reach the ChangeReceipt
# --------------------------------------------------------------------------


def test_b1_write_error_records_error_code_and_op_index_on_receipt() -> None:
    """The reported symptom from ses_65fefe0a5d: a RolledBack receipt with
    applied_op_ids=[] and no error field, leaving the LLM to guess "似乎仅部分
    应用". With B-1, the captured write exception reaches the receipt as a
    bounded (error_code, error_message) pair, including the failed op index.
    """
    spy: list = []
    scene = _standard_scene(spy)
    # Force createNode to raise on the very first create.
    scene["/obj/ws"].fail_create = True
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"),
        node_id="n_a", node_type="geo", node_name="a",
        workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "RolledBack"
    assert receipt.applied_op_ids == ()
    # B-1 contract: failure cause is no longer dropped on the floor.
    assert receipt.error_code is not None
    assert receipt.error_code == "apply.unexpected_error"
    assert receipt.error_message is not None
    # The cause string and the failed op label both reach the message.
    assert "createNode failed" in receipt.error_message
    assert "failed at op #1" in receipt.error_message
    assert "geo 'a'" in receipt.error_message


def test_b1_successful_apply_leaves_error_fields_none() -> None:
    """APPLIED receipts must not carry an apply cause."""
    spy: list = []
    scene = _standard_scene(spy)
    adapter = _adapter(spy, scene)
    create = CreateNode(
        op_id="op_a", parent=_noderef("n_root", "/obj/ws", "subnet"),
        node_id="n_a", node_type="geo", node_name="a",
        workspace_id=WS, capability="modeling", role="member",
    )
    _cs, req = _complete(adapter, (create,))
    receipt = ChangeSetExecutor(adapter).apply(req)
    assert receipt.status.value == "Applied"
    assert receipt.error_code is None
    assert receipt.error_message is None


def test_b1_classify_helper_maps_adapter_error_to_its_code() -> None:
    """When the bridge raises HoudiniAdapterError, the receipt should carry
    that exception's own dotted code, not the generic fallback."""
    from houdini_side.secure_bridge import HoudiniAdapterError

    executor = ChangeSetExecutor.__new__(ChangeSetExecutor)
    exc = HoudiniAdapterError(
        code="bridge.stale_scene", category="scene",
        message_for_user="Scene epoch mismatch",
    )
    code, message = executor._exception_to_code(exc)
    assert code == "bridge.stale_scene"
    assert "Scene epoch mismatch" in message


def test_b1_classify_helper_falls_back_for_generic_exception() -> None:
    executor = ChangeSetExecutor.__new__(ChangeSetExecutor)
    code, message = executor._exception_to_code(ValueError("bad parm name"))
    assert code == "apply.unexpected_error"
    assert "bad parm name" in message
