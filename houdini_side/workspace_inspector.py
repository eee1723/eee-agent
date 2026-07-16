"""Bounded, read-only Houdini workspace fact inspection.

``hou`` and the scene-binding reader are injected.  This module never imports
Houdini itself and never calls a mutating HOM method.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectError,
    WorkspaceInspectLimitError,
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
    WorkspaceNodeObservation,
)

_WORKSPACE_KEY = "eee.workspace_id"
_NODE_KEY = "eee.node_id"
_CAPABILITY_KEY = "eee.capability"
_ROLE_KEY = "eee.role"
_SCHEMA_KEY = "eee.schema_version"
_CREATED_BY_RUN_KEY = "eee.created_by_run"


WorkspaceInspectorError = WorkspaceInspectError


def _conflict(message: str = "The live workspace identity is ambiguous.") -> WorkspaceInspectorError:
    return WorkspaceInspectError(
        code="workspace.identity_conflict",
        category="validation",
        message_for_user=message,
        retryable=False,
    )


class WorkspaceInspector:
    """Reads exact selection or manifest facts without mutating Houdini."""

    def __init__(
        self,
        hou: object,
        *,
        binding_provider: Callable[[], SceneBinding],
        max_scan_nodes: int = 4096,
    ) -> None:
        if not callable(binding_provider):
            raise TypeError("binding_provider must be callable")
        if type(max_scan_nodes) is not int or max_scan_nodes < 1:
            raise ValueError("max_scan_nodes must be a positive exact integer")
        self._hou = hou
        self._binding_provider = binding_provider
        self._max_scan_nodes = max_scan_nodes

    def inspect(self, request: WorkspaceInspectRequest) -> WorkspaceInspectResult:
        if type(request) is not WorkspaceInspectRequest:
            raise TypeError("request must be an exact WorkspaceInspectRequest")
        binding = self._binding_provider()
        if type(binding) is not SceneBinding:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="The Houdini scene binding could not be read.",
            )
        if (
            request.scene_epoch is not None
            and request.scene_epoch != binding.scene_epoch
        ):
            raise WorkspaceInspectorError(
                code="bridge.stale_scene",
                category="stale_scene",
                message_for_user="The Houdini scene changed; refresh before continuing.",
                retryable=True,
            )

        scene_nodes = self._scan_scene()
        if request.mode == "selection":
            selected = tuple(self._hou.selectedNodes())  # type: ignore[attr-defined]
            observations = tuple(self._observe(node) for node in selected)
            target_ids = {
                observation.node_id
                for observation in observations
                if observation.node_id is not None
            }
            self._require_unique_ids(scene_nodes, target_ids)
        else:
            manifest = request.manifest
            assert manifest is not None  # guaranteed by the request contract
            target_ids = {node.node_id for node in manifest.nodes}
            matches = self._require_unique_ids(scene_nodes, target_ids)
            observations = tuple(
                self._observe(matches[node_id])
                for node_id in sorted(target_ids)
                if node_id in matches
            )

        try:
            return WorkspaceInspectResult.build(
                binding=binding,
                mode=request.mode,
                observations=observations,
                scene_may_have_changed=False,
            )
        except WorkspaceInspectLimitError as exc:
            raise WorkspaceInspectorError(
                code="bridge.result_too_large",
                category="validation",
                message_for_user="The live workspace facts exceed the bounded result size.",
                retryable=False,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise _conflict("The live workspace facts are malformed.") from exc

    def _scan_scene(self) -> tuple[object, ...]:
        try:
            root = self._hou.node("/")  # type: ignore[attr-defined]
            if root is None:
                return ()
            pending = deque(self._children(root))
            nodes: list[object] = []
            while pending:
                if len(nodes) >= self._max_scan_nodes:
                    raise WorkspaceInspectorError(
                        code="bridge.result_too_large",
                        category="validation",
                        message_for_user=(
                            "The Houdini scene is too large for bounded workspace inspection."
                        ),
                        retryable=False,
                    )
                node = pending.popleft()
                nodes.append(node)
                pending.extend(self._children(node))
        except WorkspaceInspectorError:
            raise
        except Exception as exc:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="The Houdini scene could not be inspected.",
                retryable=True,
            ) from exc
        return tuple(nodes)

    @staticmethod
    def _children(node: object) -> tuple[object, ...]:
        value = node.children()  # type: ignore[attr-defined]
        if type(value) is not tuple:
            raise TypeError("Houdini node children must be an exact tuple")
        return value

    def _require_unique_ids(
        self, scene_nodes: tuple[object, ...], target_ids: set[str]
    ) -> dict[str, object]:
        matches: dict[str, object] = {}
        for node in scene_nodes:
            node_id = self._read_user_data(node, _NODE_KEY)
            if node_id is None or node_id not in target_ids:
                continue
            if node_id in matches:
                raise _conflict()
            matches[node_id] = node
        return matches

    def _observe(self, node: object) -> WorkspaceNodeObservation:
        try:
            path = node.path()  # type: ignore[attr-defined]
            if type(path) is not str:
                raise WorkspaceInspectorError(
                    code="bridge.houdini_read_failed",
                    category="houdini_read",
                    message_for_user="A Houdini node path could not be read exactly.",
                )
            node_type = node.type().name()  # type: ignore[attr-defined]
            if type(node_type) is not str:
                raise WorkspaceInspectorError(
                    code="bridge.houdini_read_failed",
                    category="houdini_read",
                    message_for_user="A Houdini node type could not be read exactly.",
                )
            parent = node.parent()  # type: ignore[attr-defined]
            if parent is None:
                parent_path = "/"
            else:
                parent_path = parent.path()
                if type(parent_path) is not str:
                    raise WorkspaceInspectorError(
                        code="bridge.houdini_read_failed",
                        category="houdini_read",
                        message_for_user="A Houdini parent path could not be read exactly.",
                    )
            is_locked = self._is_locked(node)
            schema_raw = self._read_user_data(node, _SCHEMA_KEY)
            if schema_raw is None:
                schema_version = None
            elif schema_raw == "1":
                schema_version = 1
            else:
                raise _conflict("A live workspace schema mirror is not supported.")
            return WorkspaceNodeObservation(
                path=path,
                node_type=node_type,
                parent_path=parent_path,
                is_locked=is_locked,
                workspace_id=self._read_user_data(node, _WORKSPACE_KEY),
                node_id=self._read_user_data(node, _NODE_KEY),
                capability=self._read_user_data(node, _CAPABILITY_KEY),
                role=self._read_user_data(node, _ROLE_KEY),
                schema_version=schema_version,
                created_by_run=self._read_user_data(node, _CREATED_BY_RUN_KEY),
            )
        except WorkspaceInspectorError:
            raise
        except (TypeError, ValueError, AttributeError) as exc:
            raise _conflict("A live workspace node has malformed identity facts.") from exc
        except Exception as exc:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="A Houdini workspace node could not be read.",
                retryable=True,
            ) from exc

    @staticmethod
    def _read_user_data(node: object, key: str) -> str | None:
        try:
            value = node.userData(key)  # type: ignore[attr-defined]
        except Exception as exc:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="A Houdini workspace identity could not be read.",
                retryable=True,
            ) from exc
        if value is None:
            return None
        if type(value) is not str:
            raise _conflict("A live workspace identity value is malformed.")
        return value

    @staticmethod
    def _is_locked(node: object) -> bool:
        hard_reader = getattr(node, "isHardLocked", None)
        soft_reader = getattr(node, "isSoftLocked", None)
        if hard_reader is None and soft_reader is None:
            return False
        try:
            hard = False if hard_reader is None else hard_reader()
            soft = False if soft_reader is None else soft_reader()
        except Exception as exc:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="A Houdini node lock state could not be read.",
                retryable=True,
            ) from exc
        if type(hard) is not bool or type(soft) is not bool:
            raise WorkspaceInspectorError(
                code="bridge.houdini_read_failed",
                category="houdini_read",
                message_for_user="A Houdini node lock state is not an exact boolean.",
            )
        return hard or soft


__all__ = ["WorkspaceInspector", "WorkspaceInspectorError"]
