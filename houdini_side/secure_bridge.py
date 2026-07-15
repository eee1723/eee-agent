"""Read-only Houdini scene adapter for the secure HoudiniBridge.

This module NEVER imports ``hou`` at module import time. ``hou`` is obtained via
dependency injection (:class:`HoudiniSceneAdapter` receives a ``hou`` module) or
the :func:`create_houdini_scene_adapter` factory, which imports ``hou`` lazily
inside the Houdini process. Offline tests inject a ``fake_hou`` instead.

The adapter performs only bounded, read-only HOM reads and converts every result
into the frozen DTOs defined in :mod:`eee_agent.houdini_bridge.contracts`. It
never returns a HOM proxy, never mutates the scene, never starts a socket or a
background worker, and never imports ``rpyc`` or the Runtime.

Verified Houdini 21.0.440 API (introspected on the local install):

* ``hou.applicationVersionString()``
* ``hou.hipFile.name()`` / ``addEventCallback(callback)`` /
  ``removeEventCallback(callback)`` / ``eventCallbacks()``
* ``hou.hipFileEventType`` — ``BeforeClear``/``AfterClear``/``BeforeLoad``/
  ``AfterLoad``/``BeforeSave``/``AfterSave``/...; the callback is invoked as
  ``callback(event_type)``.
* ``hou.selectedNodes()``, ``hou.node(path)``
* ``hou.Node.path/name/type/parent``; ``hou.SopNode.isHardLocked/isSoftLocked``
  and ``geometry()``; ``hou.Geometry.pointCount/primCount/boundingBox``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence

from eee_agent.houdini_bridge.contracts import (
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)

_HIP_UNSAVED_SENTINEL = "untitled"


class HoudiniAdapterError(Exception):
    """Structured, leak-free error raised by the read-only scene adapter.

    Carries the bridge error fields so the server can translate it into a
    :class:`~eee_agent.houdini_bridge.contracts.BridgeError` response. No HOM
    object, token, or traceback is embedded in the message.
    """

    __slots__ = (
        "code",
        "category",
        "message_for_user",
        "retryable",
        "technical_detail_ref",
    )

    def __init__(
        self,
        *,
        code: str,
        category: str,
        message_for_user: str,
        retryable: bool = False,
        technical_detail_ref: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable
        self.technical_detail_ref = technical_detail_ref
        super().__init__(message_for_user)


def create_houdini_scene_adapter() -> "HoudiniSceneAdapter":
    """Build an adapter bound to the real ``hou`` module (Houdini process only)."""
    import hou  # lazy: only resolved inside Houdini

    return HoudiniSceneAdapter(hou)


class HoudiniSceneAdapter:
    """Bounded, read-only view of a Houdini scene.

    ``hou`` is injected so tests can pass a ``fake_hou``. The adapter tracks a
    scene epoch starting at 1 and increments it on load/clear events only.
    """

    def __init__(self, hou: object) -> None:
        self._hou = hou
        self._scene_epoch = 1
        self._callback = None
        self._callbacks_installed = False
        self._closed = False

    # ------------------------------------------------------------------ binding

    def binding(self) -> SceneBinding:
        """Return the current scene binding (a bounded read)."""
        instance_id = self._instance_id()
        hip_path = self._hip_path()
        selection_paths = self._selection_paths()
        revision = self._revision(
            {
                "instance_id": instance_id,
                "scene_epoch": self._scene_epoch,
                "hip_path": hip_path,
                "selection": selection_paths,
            }
        )
        return SceneBinding(
            instance_id=instance_id,
            scene_epoch=self._scene_epoch,
            hip_path=hip_path,
            observed_revision=revision,
        )

    def scene_query(
        self,
        *,
        include_selection: bool,
        node_paths: Sequence[str],
        include_geometry_stats: bool,
        expected_scene_epoch: int,
    ) -> SceneQueryResult:
        """Perform one bounded, read-only scene query.

        The epoch check happens before any scene read; a mismatch raises a
        stale-scene error immediately. ``selected_nodes`` reflects
        ``hou.selectedNodes()`` at this single read; ``nodes`` reflects only the
        explicitly requested paths.
        """
        if expected_scene_epoch != self._scene_epoch:
            raise HoudiniAdapterError(
                code="bridge.stale_scene",
                category="stale_scene",
                message_for_user="The Houdini scene changed; refresh before continuing.",
                retryable=True,
            )
        instance_id = self._instance_id()
        hip_path = self._hip_path()
        selected_nodes = (
            self._read_selected(include_geometry_stats) if include_selection else ()
        )
        nodes = self._read_requested(node_paths, include_geometry_stats)
        revision = self._revision(
            {
                "instance_id": instance_id,
                "scene_epoch": self._scene_epoch,
                "hip_path": hip_path,
                "selected": [node.to_dict() for node in selected_nodes],
                "nodes": [node.to_dict() for node in nodes],
            }
        )
        binding = SceneBinding(
            instance_id=instance_id,
            scene_epoch=self._scene_epoch,
            hip_path=hip_path,
            observed_revision=revision,
        )
        return SceneQueryResult(
            binding=binding, selected_nodes=selected_nodes, nodes=nodes
        )

    # ------------------------------------------------------ scene epoch callbacks

    def install_scene_epoch_callbacks(self) -> None:
        """Register the hipFile event callback that tracks the scene epoch.

        Idempotent. The callback only bumps the epoch on load/clear; it performs
        no scene reads and no writes. On callback failure it never silently
        forges an epoch.
        """
        if self._closed or self._callbacks_installed:
            return
        # Keep a stable reference so removal uses the exact same callable.
        self._callback = self._on_scene_event
        self._hou.hipFile.addEventCallback(self._callback)
        self._callbacks_installed = True

    def _on_scene_event(self, event_type: object) -> None:
        event_types = self._hou.hipFileEventType
        # Only load/clear change the scene identity; Save/Save As must not.
        if event_type == event_types.AfterLoad or event_type == event_types.AfterClear:
            self._scene_epoch += 1

    # --------------------------------------------------------------------- close

    def close(self) -> None:
        """Release the event callback. Idempotent, non-mutating to the scene."""
        if self._closed:
            return
        self._closed = True
        if self._callbacks_installed and self._callback is not None:
            try:
                self._hou.hipFile.removeEventCallback(self._callback)
            except Exception:  # noqa: BLE001 — closing must never raise
                pass
            self._callbacks_installed = False
            self._callback = None

    # ----------------------------------------------------------- bounded readers

    def _instance_id(self) -> str:
        version = str(self._hou.applicationVersionString())
        return f"hou:{version}:pid{os.getpid()}"

    def _hip_path(self) -> str | None:
        name = self._hou.hipFile.name()
        if not isinstance(name, str) or not name or name == _HIP_UNSAVED_SENTINEL:
            return None
        return name

    def _selection_paths(self) -> list[str]:
        paths: list[str] = []
        for node in self._hou.selectedNodes():
            try:
                paths.append(str(node.path()))
            except Exception:  # noqa: BLE001 — skip a node we cannot read
                continue
        return paths

    def _read_selected(
        self, include_geometry_stats: bool
    ) -> tuple[SelectedNode, ...]:
        nodes = []
        for node in self._hou.selectedNodes():
            nodes.append(self._to_selected_node(node, include_geometry_stats))
        return tuple(nodes)

    def _read_requested(
        self, node_paths: Sequence[str], include_geometry_stats: bool
    ) -> tuple[SelectedNode, ...]:
        nodes = []
        for path in node_paths:
            if not (isinstance(path, str) and path.startswith("/")):
                raise HoudiniAdapterError(
                    code="bridge.invalid_request",
                    category="protocol",
                    message_for_user="Requested node paths must be absolute Houdini paths.",
                )
            node = self._hou.node(path)
            if node is None:
                raise HoudiniAdapterError(
                    code="bridge.houdini_read_failed",
                    category="houdini_read",
                    message_for_user="A requested node was not found in the scene.",
                )
            nodes.append(self._to_selected_node(node, include_geometry_stats))
        return tuple(nodes)

    def _to_selected_node(
        self, node: object, include_geometry_stats: bool
    ) -> SelectedNode:
        path = str(node.path())
        node_type = str(node.type().name())
        parent = node.parent()
        parent_path = str(parent.path()) if parent is not None else "/"
        display_name = str(node.name())
        is_locked = self._read_is_locked(node)
        geometry_stats = (
            self._read_geometry_stats(node) if include_geometry_stats else None
        )
        return SelectedNode(
            path=path,
            node_type=node_type,
            parent_path=parent_path,
            display_name=display_name,
            is_locked=is_locked,
            geometry_stats=geometry_stats,
        )

    def _read_is_locked(self, node: object) -> bool:
        # isHardLocked / isSoftLocked exist on hou.SopNode; other nodes have no
        # lock concept. This is a bounded read, not arbitrary attribute access.
        try:
            return bool(node.isHardLocked() or node.isSoftLocked())
        except Exception:  # noqa: BLE001 — non-SOP nodes simply aren't locked
            return False

    def _read_geometry_stats(self, node: object) -> dict[str, object] | None:
        try:
            geometry = node.geometry()
        except Exception:  # noqa: BLE001 — node has no cookable geometry
            return None
        try:
            points = int(geometry.pointCount())
            prims = int(geometry.primCount())
        except Exception:  # noqa: BLE001 — geometry unreadable
            return None
        stats: dict[str, object] = {"points": points, "primitives": prims}
        bbox = self._read_bbox(geometry)
        if bbox is not None:
            stats["bbox"] = bbox
        return stats

    def _read_bbox(self, geometry: object) -> dict[str, list[float]] | None:
        try:
            bbox = geometry.boundingBox()
            mn = bbox.minvec()
            mx = bbox.maxvec()
        except Exception:  # noqa: BLE001 — empty geometry has no bounding box
            return None
        return {
            "min": [mn.x(), mn.y(), mn.z()],
            "max": [mx.x(), mx.y(), mx.z()],
        }

    # ------------------------------------------------------------- revision hash

    def _revision(self, facts: dict[str, object]) -> str:
        """Deterministic read hash over bounded scene facts (evidence only)."""
        text = json.dumps(
            facts,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
