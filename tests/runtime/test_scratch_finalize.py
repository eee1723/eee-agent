"""Offline tests for scratch commit finalization and lifecycle operations."""

from __future__ import annotations

import contextlib

import pytest

from eee_agent.houdini_bridge.scratch import (
    ScratchDeleteRequest,
    ScratchExpr,
    ScratchOp,
    ScratchRequest,
    ScratchTopologyRequest,
)
from houdini_side.changeset_executor import ChangeSetExecutor, _layered_layout
from houdini_side.secure_bridge import HoudiniAdapterError, HoudiniSceneAdapter


class _Category:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Type:
    def __init__(self, name: str, category: str = "Sop") -> None:
        self._name, self._category = name, category

    def name(self) -> str:
        return self._name

    def category(self) -> _Category:
        return _Category(self._category)


class _Parent:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _Conn:
    def __init__(self, source: "_Node", index: int = 0) -> None:
        self._source, self._index = source, index

    def outputNode(self) -> "_Node":
        return self._source

    def outputIndex(self) -> int:
        return self._index

    def inputIndex(self) -> int:
        return 0


class _ParmTemplate:
    def __init__(self, template_type: str) -> None:
        self._template_type = template_type

    def type(self) -> str:
        return self._template_type


class _Parm:
    """Minimal hou.Parm stand-in: literal set + setExpression capture."""

    def __init__(self, template_type: str = "Float") -> None:
        self._template_type = template_type
        self.value: object = None
        self.expression: tuple[str, object] | None = None

    def set(self, value: object) -> None:
        self.value = value

    def setExpression(self, expression: str, language: object = None) -> None:
        self.expression = (expression, language)

    def parmTemplate(self) -> _ParmTemplate:
        return _ParmTemplate(self._template_type)


class _Node:
    def __init__(
        self,
        scene: dict[str, "_Node"],
        spy: list[tuple],
        path: str,
        type_name: str,
        parent: str,
        category: str = "Sop",
    ) -> None:
        self.scene, self.spy = scene, spy
        self._path, self._type_name, self._parent = path, type_name, parent
        self._category = category
        self._children: list[_Node] = []
        self._inputs: dict[int, _Node] = {}
        self._outputs: list[_Node] = []
        self._position = (0.0, 0.0)
        self._display_flag = False
        self._render_flag = False
        self._comment = ""
        self._generic_flags: dict[object, bool] = {}
        self._parms: dict[str, _Parm] = {}
        self.fail_children = False
        self.fail_destroy = False

    def path(self) -> str:
        return self._path

    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    def type(self) -> _Type:
        return _Type(self._type_name, self._category)

    def parent(self) -> _Parent:
        return _Parent(self._parent)

    def children(self) -> list["_Node"]:
        if self.fail_children:
            raise RuntimeError("children read failed")
        return list(self._children)

    def createNode(self, type_name: str, name: str) -> "_Node":
        path = self._path.rstrip("/") + "/" + name
        node = _Node(self.scene, self.spy, path, type_name, self._path)
        self._children.append(node)
        self.scene[path] = node
        self.spy.append(("createNode", path))
        return node

    def position(self) -> tuple[float, float]:
        return self._position

    def setPosition(self, pos: object) -> None:
        self._position = (float(pos[0]), float(pos[1]))  # type: ignore[index]

    def setDisplayFlag(self, on: bool) -> None:
        self._display_flag = on

    def isDisplayFlagSet(self) -> bool:
        return self._display_flag

    def setRenderFlag(self, on: bool) -> None:
        self._render_flag = on

    def setComment(self, text: str) -> None:
        self._comment = text

    def setGenericFlag(self, flag: object, on: bool) -> None:
        self._generic_flags[flag] = on

    def add_parm(self, name: str, template_type: str = "Float") -> _Parm:
        parm = _Parm(template_type)
        self._parms[name] = parm
        return parm

    def parm(self, name: str) -> _Parm | None:
        return self._parms.get(name)

    def inputConnections(self) -> list[_Conn]:
        return [_Conn(node) for _, node in sorted(self._inputs.items())]

    def inputs(self) -> tuple:
        # Mirrors hou.Node.inputs(): positional, None for unconnected inputs.
        if not self._inputs:
            return ()
        return tuple(self._inputs.get(i) for i in range(max(self._inputs) + 1))

    def outputs(self) -> list["_Node"]:
        return list(self._outputs)

    def setInput(self, index: int, source: "_Node | None", out_idx: int = 0) -> None:
        del out_idx
        old = self._inputs.pop(index, None)
        if old is not None and self in old._outputs:
            old._outputs.remove(self)
        if source is not None:
            self._inputs[index] = source
            if self not in source._outputs:
                source._outputs.append(self)

    def destroy(self) -> None:
        if self.fail_destroy:
            raise RuntimeError("destroy failed")
        self.scene.pop(self._path, None)
        if self in self._outputs:
            self._outputs.clear()
        for source in self._inputs.values():
            if self in source._outputs:
                source._outputs.remove(self)


class _Undos:
    def __init__(self, spy: list[tuple]) -> None:
        self.spy = spy

    def group(self, label: str):
        @contextlib.contextmanager
        def group():
            self.spy.append(("undo_group_begin", label))
            try:
                yield
            finally:
                self.spy.append(("undo_group_end", label))

        return group()


class _Hip:
    def name(self) -> str:
        return "fake.hip"

    def addEventCallback(self, callback: object) -> None:
        del callback

    def removeEventCallback(self, callback: object) -> None:
        del callback


class _Hou:
    class nodeFlag:
        DisplayComment = "DisplayComment"

    class hipFileEventType:
        AfterClear = "AfterClear"
        AfterLoad = "AfterLoad"

    class parmTemplateType:
        Float = "Float"
        Int = "Int"
        String = "String"

    class exprLanguage:
        Hscript = "Hscript"
        Python = "Python"

    def __init__(self, scene: dict[str, _Node], spy: list[tuple]) -> None:
        self.scene, self.undos, self.hipFile = scene, _Undos(spy), _Hip()

    def applicationVersionString(self) -> str:
        return "21.0.440"

    def selectedNodes(self) -> tuple:
        return ()

    def node(self, path: str) -> _Node | None:
        return self.scene.get(path)


def _executor() -> tuple[ChangeSetExecutor, dict[str, _Node], list[tuple]]:
    scene: dict[str, _Node] = {}
    spy: list[tuple] = []
    scene["/obj"] = _Node(scene, spy, "/obj", "obj", "/", "Obj")
    return ChangeSetExecutor(HoudiniSceneAdapter(_Hou(scene, spy))), scene, spy


def _container(scene: dict[str, _Node]) -> _Node:
    root = scene["/obj"].createNode("geo", "table1")
    box = root.createNode("box", "box1")
    out = root.createNode("xform", "xform1")
    out.setInput(0, box)
    return root


def test_scratch_output_node_prefers_terminal_sink_over_accidental_flag() -> None:
    # scratch_exec never sets the display flag, so the flag in a sandbox is
    # the accidental creation default and can sit on a mid-chain node. The
    # commit output must be the terminal sink instead.
    executor, scene, _ = _executor()
    container = _container(scene)
    box = scene["/obj/table1/box1"]
    box.setDisplayFlag(True)
    output = executor._scratch_output_node(container)
    assert output.path() == "/obj/table1/xform1"


def test_scratch_output_node_prefers_connected_sink_when_ambiguous() -> None:
    # Two sinks: the chain end (xform1, one wired input) vs a disconnected
    # sphere (no inputs). The connected sink wins even when the accidental
    # creation-default flag sits on a mid-chain node — a stray disconnected
    # node must never hijack the commit output.
    executor, scene, _ = _executor()
    container = _container(scene)
    container.createNode("sphere", "sphere1")
    box = scene["/obj/table1/box1"]
    box.setDisplayFlag(True)
    assert executor._scratch_output_node(container).path() == "/obj/table1/xform1"


def test_scratch_output_node_falls_back_to_flag_holder_when_all_sinks_bare() -> None:
    # Every sink is disconnected (no inputs anywhere): resolution falls back
    # to the display-flag holder, then the last child.
    executor, scene, _ = _executor()
    root = scene["/obj"].createNode("geo", "t2")
    first = root.createNode("box", "a1")
    second = root.createNode("box", "b1")
    first.setDisplayFlag(True)
    assert executor._scratch_output_node(root).path() == first.path()
    first.setDisplayFlag(False)
    assert executor._scratch_output_node(root).path() == second.path()


def test_layered_layout_is_deterministic_and_cycle_safe() -> None:
    edges = {"/obj/t/a": (), "/obj/t/b": ("/obj/t/a",)}
    assert _layered_layout(["/obj/t/a", "/obj/t/b"], edges, anchor=(1, 2)) == {
        "/obj/t/a": (1.0, 2.0),
        "/obj/t/b": (4.0, 2.0),
    }
    cycle = {"/obj/t/a": ("/obj/t/b",), "/obj/t/b": ("/obj/t/a",)}
    assert set(_layered_layout(["/obj/t/a", "/obj/t/b"], cycle, anchor=(0, 0))) == {
        "/obj/t/a",
        "/obj/t/b",
    }


def test_finalize_layout_flags_comments_and_best_effort_warning() -> None:
    executor, scene, _ = _executor()
    container = _container(scene)
    box, output = scene["/obj/table1/box1"], scene["/obj/table1/xform1"]
    warnings = executor._finalize_commit(
        container, output, "/obj/table1", (("box1", "draft"),)
    )
    assert warnings == []
    assert output._position == (3.0, 0.0)
    assert output._display_flag and output._render_flag
    assert box._comment == "draft"
    assert box._generic_flags["DisplayComment"] is True
    container.fail_children = True
    warnings = executor._finalize_commit(container, output, "/obj/table1", ())
    assert warnings and warnings[0].startswith("layout skipped:")


def test_delete_nodes_allowlist_and_external_reference_fail_closed() -> None:
    executor, scene, spy = _executor()
    container = _container(scene)
    draft = container.createNode("box", "draft1")
    req = ScratchDeleteRequest.build(
        request_id="r", deadline_ms=5000, scene_epoch=executor.binding().scene_epoch,
        allowed_paths=(draft.path(),), paths=(draft.path(),),
    )
    result = executor.delete_nodes(req)
    assert result.deleted_paths == (draft.path(),)
    assert any(x == ("undo_group_begin", "EEE Agent - cleanup") for x in spy)

    box = scene["/obj/table1/box1"]
    consumer = container.createNode("null", "keeper1")
    consumer.setInput(0, box)
    req = ScratchDeleteRequest.build(
        request_id="r2", deadline_ms=5000, scene_epoch=executor.binding().scene_epoch,
        allowed_paths=(box.path(),), paths=(box.path(),),
    )
    result = executor.delete_nodes(req)
    assert result.deleted_paths == ()
    assert "still referenced" in result.skipped[0]["reason"]


def test_scratch_topology_reports_live_edges_and_missing_nodes() -> None:
    executor, scene, _ = _executor()
    _container(scene)
    req = ScratchTopologyRequest.build(
        request_id="r", deadline_ms=5000, scene_epoch=executor.binding().scene_epoch,
        paths=("/obj/table1/box1", "/obj/table1/xform1", "/obj/table1/nope"),
    )
    result = executor.scratch_topology(req)
    by_path = {n["path"]: n for n in result.nodes}
    assert by_path["/obj/table1/box1"]["outputs"] == ["/obj/table1/xform1"]
    assert by_path["/obj/table1/xform1"]["inputs"] == ["/obj/table1/box1"]
    assert by_path["/obj/table1/nope"]["exists"] is False


def test_scratch_delete_node_op_removes_existing_sandbox_node() -> None:
    executor, scene, _ = _executor()
    epoch = executor.binding().scene_epoch
    built = ScratchRequest.build(
        request_id="b", deadline_ms=5000, scene_epoch=epoch, sandbox_id="run1",
        operations=(
            ScratchOp(kind="create_node", node_name="box1", node_type="box"),
            ScratchOp(kind="create_node", node_name="draft1", node_type="box"),
        ), purpose="build",
    )
    executor.scratch_exec(built)
    deleted = ScratchRequest.build(
        request_id="d", deadline_ms=5000, scene_epoch=epoch, sandbox_id="run1",
        operations=(ScratchOp(kind="delete_node", node_name="draft1"),),
        purpose="cleanup",
    )
    executor.scratch_exec(deleted)
    assert scene.get("/obj/eee_scratch_run1/draft1") is None


# --------------------------------------------------------------------------
# scratch.v2 C1: typed parameter expressions on set_parm
# --------------------------------------------------------------------------


def _expr_request(
    executor: ChangeSetExecutor,
    operations: tuple[ScratchOp, ...],
    *,
    sandbox_id: str = "expr1",
) -> ScratchRequest:
    return ScratchRequest.build(
        request_id="expr_req", deadline_ms=5000,
        scene_epoch=executor.binding().scene_epoch, sandbox_id=sandbox_id,
        operations=operations, purpose="expr test",
    )


def _expr_boxes(
    executor: ChangeSetExecutor, scene: dict[str, _Node]
) -> tuple[_Node, _Node]:
    built = executor.scratch_exec(_expr_request(executor, (
        ScratchOp(kind="create_node", node_name="box1", node_type="box"),
        ScratchOp(kind="create_node", node_name="box2", node_type="box"),
    )))
    assert built.applied_ops == 2
    box1 = scene["/obj/eee_scratch_expr1/box1"]
    box2 = scene["/obj/eee_scratch_expr1/box2"]
    box1.add_parm("sizex").set(1.0)
    box2.add_parm("sizex")
    return box1, box2


def test_set_parm_expr_renders_and_resolves_relative_ref() -> None:
    executor, scene, _ = _executor()
    _, box2 = _expr_boxes(executor, scene)
    executor.scratch_exec(_expr_request(executor, (
        ScratchOp(
            kind="set_parm", node_name="box2", parm="sizex",
            expr=ScratchExpr(kind="op", name="mul", args=(
                ScratchExpr(kind="ref", path="../box1/sizex"),
                ScratchExpr(kind="num", value=2.0),
            )),
        ),
    )))
    target = box2.parm("sizex")
    assert target is not None and target.expression is not None
    rendered, language = target.expression
    assert rendered == '(ch("/obj/eee_scratch_expr1/box1/sizex") * 2.0)'
    assert language == _Hou.exprLanguage.Hscript


def test_set_parm_expr_absolute_ref_inside_sandbox() -> None:
    executor, scene, _ = _executor()
    _, box2 = _expr_boxes(executor, scene)
    executor.scratch_exec(_expr_request(executor, (
        ScratchOp(
            kind="set_parm", node_name="box2", parm="sizex",
            expr=ScratchExpr(
                kind="ref", path="/obj/eee_scratch_expr1/box1/sizex"
            ),
        ),
    )))
    target = box2.parm("sizex")
    assert target is not None and target.expression is not None
    assert target.expression[0] == 'ch("/obj/eee_scratch_expr1/box1/sizex")'


def test_set_parm_expr_escaping_ref_fails_closed() -> None:
    executor, scene, _ = _executor()
    _expr_boxes(executor, scene)
    for ref in ("../../box1/sizex", "/obj/box1/sizex"):
        with pytest.raises(HoudiniAdapterError, match="escapes the sandbox"):
            executor.scratch_exec(_expr_request(executor, (
                ScratchOp(
                    kind="set_parm", node_name="box2", parm="sizex",
                    expr=ScratchExpr(kind="ref", path=ref),
                ),
            )))


def test_set_parm_expr_missing_ref_node_or_parm_fails() -> None:
    executor, scene, _ = _executor()
    _expr_boxes(executor, scene)
    with pytest.raises(HoudiniAdapterError, match="node not found"):
        executor.scratch_exec(_expr_request(executor, (
            ScratchOp(
                kind="set_parm", node_name="box2", parm="sizex",
                expr=ScratchExpr(kind="ref", path="../ghost1/sizex"),
            ),
        )))
    with pytest.raises(HoudiniAdapterError, match="parm not found"):
        executor.scratch_exec(_expr_request(executor, (
            ScratchOp(
                kind="set_parm", node_name="box2", parm="sizex",
                expr=ScratchExpr(kind="ref", path="../box1/nope"),
            ),
        )))


def test_set_parm_expr_rejects_non_numeric_target() -> None:
    executor, scene, _ = _executor()
    _, box2 = _expr_boxes(executor, scene)
    box2.add_parm("label", template_type="String")
    with pytest.raises(HoudiniAdapterError, match="not numeric"):
        executor.scratch_exec(_expr_request(executor, (
            ScratchOp(
                kind="set_parm", node_name="box2", parm="label",
                expr=ScratchExpr(kind="num", value=1.0),
            ),
        )))


def test_set_parm_expr_func_and_neg_render() -> None:
    executor, scene, _ = _executor()
    _, box2 = _expr_boxes(executor, scene)
    executor.scratch_exec(_expr_request(executor, (
        ScratchOp(
            kind="set_parm", node_name="box2", parm="sizex",
            expr=ScratchExpr(kind="func", name="clamp", args=(
                ScratchExpr(kind="op", name="neg", args=(
                    ScratchExpr(kind="ref", path="sizex"),
                )),
                ScratchExpr(kind="num", value=-4),
                ScratchExpr(kind="func", name="max", args=(
                    ScratchExpr(kind="num", value=1),
                    ScratchExpr(kind="num", value=2.5),
                )),
            )),
        ),
    )))
    target = box2.parm("sizex")
    assert target is not None and target.expression is not None
    assert target.expression[0] == (
        'clamp((-ch("/obj/eee_scratch_expr1/box2/sizex")), -4, max(1, 2.5))'
    )
