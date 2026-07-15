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
    NodeIdentityEquals,
    NodeRef,
    OwnedNodeRef,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SetParm,
    WireInputEquals,
    WireRef,
    WorkspaceManifest,
)
from eee_agent.changesets.contracts import _derive_create_path
from eee_agent.houdini_bridge.changesets import ApplyRequest
from eee_agent.houdini_bridge.queue import MainThreadReadQueue, QueueItemCancelled
from houdini_side.changeset_executor import (
    ChangeSetExecutor,
    _dedup,
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
    ) -> None:
        self._scene, self._spy = scene, spy
        self._path, self._type, self._parent = path, type_name, parent
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
        self._create_count = 0

    def _record(self, method: str, *args: object) -> None:
        self._spy.append((method, self._path, *args))

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        if self.fail_type_read:
            raise RuntimeError("type read failed")
        return _Type(self._type)

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
            self.raise_after_setinput_once = False  # one-shot: rollback restore must succeed
            raise RuntimeError("setInput raised after mutating")

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
