"""Read-only fake-Houdini tests for Task 16-B2b-1 workspace inspection."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.houdini_bridge.workspaces import WorkspaceInspectRequest
from houdini_side.workspace_inspector import (
    WorkspaceInspector,
    WorkspaceInspectorError,
)

SES = f"ses_{'0' * 32}"
RUN = f"run_{'1' * 32}"
WS = f"ws_{'2' * 32}"
NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
MIRRORS = {
    "eee.workspace_id": WS,
    "eee.node_id": "node_root",
    "eee.capability": "modeling",
    "eee.role": "root",
    "eee.schema_version": "1",
    "eee.created_by_run": RUN,
}


class _Type:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Node:
    def __init__(
        self,
        path: str,
        *,
        node_type: str = "geo",
        parent: "_Node | None" = None,
        user_data: dict[str, str] | None = None,
        hard_locked: bool = False,
        soft_locked: bool = False,
    ) -> None:
        self._path = path
        self._type = _Type(node_type)
        self._parent = parent
        self._user_data = dict(user_data or {})
        self._hard_locked = hard_locked
        self._soft_locked = soft_locked
        self._children: list[_Node] = []
        self.mutation_calls: list[str] = []
        if parent is not None:
            parent._children.append(self)

    def path(self) -> str:
        return self._path

    def type(self) -> _Type:
        return self._type

    def parent(self) -> "_Node | None":
        return self._parent

    def userData(self, key: str) -> str | None:
        return self._user_data.get(key)

    def isHardLocked(self) -> bool:
        return self._hard_locked

    def isSoftLocked(self) -> bool:
        return self._soft_locked

    def allSubChildren(self) -> tuple["_Node", ...]:
        out: list[_Node] = []
        pending = list(self._children)
        while pending:
            node = pending.pop(0)
            out.append(node)
            pending[0:0] = node._children
        return tuple(out)

    def children(self) -> tuple["_Node", ...]:
        return tuple(self._children)

    # Any accidental write makes the test fail at the point of mutation.
    def _mutate(self, name: str) -> None:
        self.mutation_calls.append(name)
        raise AssertionError(f"workspace inspection attempted mutation: {name}")

    def createNode(self, *args: object, **kwargs: object) -> None:
        self._mutate("createNode")

    def destroy(self) -> None:
        self._mutate("destroy")

    def setInput(self, *args: object, **kwargs: object) -> None:
        self._mutate("setInput")

    def setUserData(self, *args: object, **kwargs: object) -> None:
        self._mutate("setUserData")


class _Hou:
    def __init__(self, root: _Node, *, selected: tuple[_Node, ...] = ()) -> None:
        self._root = root
        self._selected = selected

    def node(self, path: str) -> _Node | None:
        if path == "/":
            return self._root
        return next(
            (node for node in self._root.allSubChildren() if node.path() == path),
            None,
        )

    def selectedNodes(self) -> tuple[_Node, ...]:
        return self._selected


def _binding(epoch: int = 3) -> SceneBinding:
    return SceneBinding(
        instance_id="hou:21.0.440:pid123",
        scene_epoch=epoch,
        hip_path=None,
        observed_revision="sha256:scene",
    )


def _request(
    *, mode: str = "selection", manifest: WorkspaceManifest | None = None, epoch: int | None = 3
) -> WorkspaceInspectRequest:
    return WorkspaceInspectRequest(
        request_id="req_workspace_inspector",
        deadline_ms=5000,
        scene_epoch=epoch,
        mode=mode,
        manifest=manifest,
    )


def _manifest(path: str = "/obj/eee/root") -> WorkspaceManifest:
    root = OwnedNodeRef(
        node_id="node_root",
        path=path,
        node_type="geo",
        parent_path="/obj/eee",
        capability="modeling",
        role="root",
    )
    return WorkspaceManifest.build(
        workspace_id=WS,
        session_id=SES,
        instance_id="hou:21.0.440:pid123",
        scene_epoch=3,
        roots=(root,),
        nodes=(root,),
        created_by_run=RUN,
        updated_at=NOW,
    )


def _scene(*, selected: bool = True, mirrors: dict[str, str] | None = None) -> tuple[_Hou, _Node, _Node]:
    root = _Node("/")
    obj = _Node("/obj", parent=root)
    eee = _Node("/obj/eee", parent=obj)
    owned = _Node("/obj/eee/root", parent=eee, user_data=MIRRORS if mirrors is None else mirrors)
    _Node("/obj/eee/root/child", node_type="box", parent=owned)
    return _Hou(root, selected=(owned,) if selected else ()), root, owned


def test_selection_returns_exact_selected_node_without_descendants_and_no_writes() -> None:
    hou, root, owned = _scene()
    result = WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())

    assert result.mode == "selection"
    assert len(result.observations) == 1
    fact = result.observations[0]
    assert fact.path == "/obj/eee/root"
    assert fact.node_id == "node_root"
    assert fact.workspace_id == WS
    assert fact.schema_version == 1
    assert fact.created_by_run == RUN
    assert all(not node.mutation_calls for node in (root, *root.allSubChildren()))


def test_empty_selection_is_a_valid_empty_observation_set() -> None:
    hou, _root, _owned = _scene(selected=False)
    result = WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert result.observations == ()


def test_partial_mirrors_are_reported_as_none_for_service_validation() -> None:
    hou, _root, _owned = _scene(mirrors={"eee.workspace_id": WS})
    fact = WorkspaceInspector(hou, binding_provider=_binding).inspect(_request()).observations[0]
    assert fact.workspace_id == WS
    assert fact.node_id is None
    assert fact.capability is None
    assert fact.schema_version is None


def test_manifest_mode_ignores_selection_and_resolves_stable_id_after_rename() -> None:
    hou, _root, owned = _scene(selected=False)
    owned._path = "/obj/eee/renamed"
    result = WorkspaceInspector(hou, binding_provider=_binding).inspect(
        _request(mode="manifest", manifest=_manifest())
    )
    assert tuple(item.node_id for item in result.observations) == ("node_root",)
    assert result.observations[0].path == "/obj/eee/renamed"


def test_manifest_mode_omits_missing_id_so_service_can_report_stale() -> None:
    hou, _root, _owned = _scene(mirrors={})
    result = WorkspaceInspector(hou, binding_provider=_binding).inspect(
        _request(mode="manifest", manifest=_manifest())
    )
    assert result.observations == ()


def test_duplicate_stable_id_anywhere_in_scene_fails_closed() -> None:
    hou, _root, owned = _scene()
    duplicate = _Node(
        "/obj/eee/copied",
        parent=owned.parent(),
        user_data=MIRRORS,
    )
    assert duplicate.path() != owned.path()
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "workspace.identity_conflict"


def test_stale_epoch_fails_before_scene_scan() -> None:
    hou, root, _owned = _scene()
    root._children = _ExplodingList()
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request(epoch=2))
    assert exc.value.code == "bridge.stale_scene"


class _ExplodingList(list[_Node]):
    def __iter__(self):  # type: ignore[no-untyped-def]
        raise AssertionError("scene scanned before stale epoch rejection")


def test_scan_bound_fails_closed() -> None:
    hou, _root, _owned = _scene()
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding, max_scan_nodes=2).inspect(
            _request()
        )
    assert exc.value.code == "bridge.result_too_large"


def test_lock_state_is_read_only_fact() -> None:
    hou, _root, owned = _scene()
    owned._soft_locked = True
    fact = WorkspaceInspector(hou, binding_provider=_binding).inspect(_request()).observations[0]
    assert fact.is_locked is True


def test_invalid_schema_user_data_is_reported_as_conflict() -> None:
    mirrors = dict(MIRRORS)
    mirrors["eee.schema_version"] = "not-an-int"
    hou, _root, _owned = _scene(mirrors=mirrors)
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "workspace.identity_conflict"


@pytest.mark.parametrize("raw", ["01", "+1", " 1", "1 ", "1.0", "2", ""])
def test_noncanonical_or_unsupported_schema_is_not_accepted_as_schema_one(
    raw: str,
) -> None:
    mirrors = dict(MIRRORS)
    mirrors["eee.schema_version"] = raw
    hou, _root, _owned = _scene(mirrors=mirrors)
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "workspace.identity_conflict"


def test_lock_read_failure_is_not_silently_treated_as_unlocked() -> None:
    hou, _root, owned = _scene()

    def broken_lock() -> bool:
        raise RuntimeError("lock read failed")

    owned.isHardLocked = broken_lock  # type: ignore[method-assign]
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "bridge.houdini_read_failed"


def test_non_string_hom_path_is_not_coerced_to_text() -> None:
    hou, _root, owned = _scene()

    class _LooksLikePath:
        def __str__(self) -> str:
            return "/obj/forged"

    owned.path = lambda: _LooksLikePath()  # type: ignore[method-assign]
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "bridge.houdini_read_failed"


def test_truthy_non_bool_lock_result_is_not_coerced() -> None:
    hou, _root, owned = _scene()
    owned.isHardLocked = lambda: 1  # type: ignore[method-assign,return-value]
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding).inspect(_request())
    assert exc.value.code == "bridge.houdini_read_failed"


def test_scan_uses_bounded_child_walk_not_eager_all_subchildren() -> None:
    hou, root, _owned = _scene()

    def forbidden_eager_scan() -> tuple[_Node, ...]:
        raise AssertionError("allSubChildren eagerly materializes the full scene")

    root.allSubChildren = forbidden_eager_scan  # type: ignore[method-assign]
    with pytest.raises(WorkspaceInspectorError) as exc:
        WorkspaceInspector(hou, binding_provider=_binding, max_scan_nodes=2).inspect(
            _request()
        )
    assert exc.value.code == "bridge.result_too_large"


def test_inspector_source_contains_no_houdini_mutation_calls() -> None:
    import ast
    import inspect

    from houdini_side import workspace_inspector

    forbidden = {
        "createNode",
        "destroy",
        "set",
        "setInput",
        "setUserData",
        "setDisplayFlag",
        "setRenderFlag",
        "setBypass",
        "setHardLocked",
        "setSoftLocked",
        "save",
        "load",
        "clear",
        "installFile",
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(ast.parse(inspect.getsource(workspace_inspector)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_attributes.isdisjoint(forbidden)
