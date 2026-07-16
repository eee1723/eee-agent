"""Provider-independent trusted Workspace lifecycle orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Protocol

from eee_agent.changesets.contracts import OwnedNodeRef, WorkspaceManifest
from eee_agent.changesets.repository import (
    ChangeSetRepository,
    WorkspaceMutationResult,
    WorkspaceStateRecord,
)
from eee_agent.core import AgentError, AgentException, ErrorCategory, IdKind, require_id
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
    WorkspaceNodeObservation,
)
from eee_agent.runtime.models import canonical_json_dumps

_MAX_SESSION_WORKSPACES = 1024
_MAX_INSPECTION_SUMMARY_BYTES = 512 * 1024


class WorkspaceFactProvider(Protocol):
    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult: ...

    async def inspect_manifest(
        self,
        manifest: WorkspaceManifest,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectResult: ...


class WorkspaceHealth(StrEnum):
    HEALTHY = "Healthy"
    STALE = "Stale"
    CONFLICT = "Conflict"
    BRIDGE_UNAVAILABLE = "BridgeUnavailable"


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace_id: str
    revision: str
    node_count: int
    instance_id: str
    scene_epoch: int
    active: bool

    def __post_init__(self) -> None:
        _require_id_value(self.workspace_id, IdKind.WORKSPACE, "workspace_id")
        _require_revision(self.revision)
        if type(self.node_count) is not int:
            raise TypeError("node_count must be an exact integer")
        if self.node_count < 1 or self.node_count > 4096:
            raise ValueError("node_count must be in 1..4096")
        if type(self.instance_id) is not str:
            raise TypeError("instance_id must be an exact string")
        if (
            not self.instance_id
            or len(self.instance_id) > 128
            or any(ord(ch) < 32 for ch in self.instance_id)
        ):
            raise ValueError("instance_id must be bounded text")
        if type(self.scene_epoch) is not int:
            raise TypeError("scene_epoch must be an exact integer")
        if self.scene_epoch < 1:
            raise ValueError("scene_epoch must be >= 1")
        if type(self.active) is not bool:
            raise TypeError("active must be an exact bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "revision": self.revision,
            "node_count": self.node_count,
            "instance_id": self.instance_id,
            "scene_epoch": self.scene_epoch,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceLifecycleSummary:
    workspace: WorkspaceSummary
    active_workspace_id: str | None
    state_revision: int | None
    changed: bool

    def __post_init__(self) -> None:
        if type(self.workspace) is not WorkspaceSummary:
            raise TypeError("workspace must be an exact WorkspaceSummary")
        if self.active_workspace_id is not None:
            _require_id_value(
                self.active_workspace_id, IdKind.WORKSPACE, "active_workspace_id"
            )
        if self.state_revision is not None:
            if type(self.state_revision) is not int:
                raise TypeError("state_revision must be an exact integer or None")
            if self.state_revision < 1:
                raise ValueError("state_revision must be >= 1")
        if (self.active_workspace_id is None) != (self.state_revision is None):
            raise ValueError("active workspace and state revision must be paired")
        if type(self.changed) is not bool:
            raise TypeError("changed must be an exact bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace": self.workspace.to_dict(),
            "active_workspace_id": self.active_workspace_id,
            "state_revision": self.state_revision,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceInspectionSummary:
    workspaces: tuple[WorkspaceSummary, ...]
    active_workspace_id: str | None
    target_manifest: WorkspaceManifest
    status: WorkspaceHealth

    def __post_init__(self) -> None:
        if type(self.workspaces) is not tuple or any(
            type(workspace) is not WorkspaceSummary for workspace in self.workspaces
        ):
            raise TypeError("workspaces must be a tuple of exact WorkspaceSummary values")
        if len(self.workspaces) > _MAX_SESSION_WORKSPACES:
            raise ValueError("workspaces exceed the bounded Session summary count")
        ids = [workspace.workspace_id for workspace in self.workspaces]
        if len(ids) != len(set(ids)):
            raise ValueError("workspaces contain a duplicate workspace id")
        if self.active_workspace_id is not None:
            _require_id_value(
                self.active_workspace_id, IdKind.WORKSPACE, "active_workspace_id"
            )
            if self.active_workspace_id not in set(ids):
                raise ValueError("active_workspace_id must be present in workspaces")
        if type(self.target_manifest) is not WorkspaceManifest:
            raise TypeError("target_manifest must be an exact WorkspaceManifest")
        if self.target_manifest.workspace_id not in set(ids):
            raise ValueError("target_manifest must be present in workspaces")
        if type(self.status) is not WorkspaceHealth:
            raise TypeError("status must be an exact WorkspaceHealth")
        if (
            len(canonical_json_dumps(self.to_dict()).encode("utf-8"))
            > _MAX_INSPECTION_SUMMARY_BYTES
        ):
            raise ValueError("workspace inspection summary exceeds the bounded size")

    def to_dict(self) -> dict[str, object]:
        return {
            "workspaces": [workspace.to_dict() for workspace in self.workspaces],
            "active_workspace_id": self.active_workspace_id,
            "target_manifest": self.target_manifest.to_dict(),
            "status": self.status.value,
        }


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _error(code: str, message: str) -> AgentException:
    return AgentException(
        AgentError(
            code=code,
            category=ErrorCategory.VALIDATION,
            message_for_user=message,
        )
    )


def _no_selection() -> AgentException:
    return _error("workspace.no_selection", "No Houdini nodes are selected.")


def _unowned_selection() -> AgentException:
    return _error(
        "workspace.unowned_selection",
        "The selection does not contain trusted EEE-owned nodes.",
    )


def _incomplete_selection() -> AgentException:
    return _error(
        "workspace.incomplete_selection",
        "The selection has incomplete or unsupported EEE identity.",
    )


def _mixed_selection() -> AgentException:
    return _error(
        "workspace.mixed_selection",
        "The selection mixes multiple trusted workspace identities.",
    )


def _identity_conflict() -> AgentException:
    return _error(
        "workspace.identity_conflict",
        "The live workspace identity is ambiguous or conflicts with persistence.",
    )


def _session_mismatch() -> AgentException:
    return _error(
        "workspace.session_mismatch", "The workspace belongs to another Session."
    )


def _revision_conflict() -> AgentException:
    return _error(
        "workspace.revision_conflict",
        "The workspace manifest no longer matches the expected revision.",
    )


def _active_conflict() -> AgentException:
    return _error(
        "workspace.active_conflict",
        "The active workspace changed before the request completed.",
    )


def _locked_selection() -> AgentException:
    return _error(
        "workspace.locked_selection",
        "A selected node is locked and cannot be registered as a usable workspace.",
    )


def _workspace_not_found() -> AgentException:
    return _error("workspace.not_found", "The workspace does not exist.")


def _bridge_unavailable() -> AgentException:
    return _error(
        "bridge.capability_unavailable",
        "Trusted workspace inspection is not currently available.",
    )


async def _get_workspace_public(
    repository: ChangeSetRepository, workspace_id: str
) -> WorkspaceManifest:
    try:
        return await repository.get_workspace(workspace_id)
    except AgentException as exc:
        if exc.error.code == "runtime.workspace_not_found":
            raise _workspace_not_found() from exc
        raise


def _require_epoch(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError("expected_scene_epoch must be an exact integer or None")
    if value < 1:
        raise ValueError("expected_scene_epoch must be >= 1")
    return value


def _require_id_value(value: object, kind: IdKind, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string")
    return require_id(value, kind)


def _require_revision(value: object) -> str:
    if type(value) is not str:
        raise TypeError("expected_manifest_revision must be an exact string")
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("expected_manifest_revision must be a lowercase SHA-256")
    return value


def _require_utc(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _complete_selection(
    result: WorkspaceInspectResult, *, allow_locked: bool
) -> tuple[WorkspaceNodeObservation, ...]:
    if type(result) is not WorkspaceInspectResult:
        raise TypeError("provider must return an exact WorkspaceInspectResult")
    if result.mode != "selection":
        raise ValueError("selection inspection returned the wrong mode")
    observations = result.observations
    if not observations:
        raise _no_selection()
    ownership_fields = tuple(
        (
            item.workspace_id,
            item.node_id,
            item.capability,
            item.role,
            item.schema_version,
            item.created_by_run,
        )
        for item in observations
    )
    if all(all(value is None for value in fields) for fields in ownership_fields):
        raise _unowned_selection()
    if any(any(value is None for value in fields) for fields in ownership_fields):
        raise _incomplete_selection()
    if any(item.schema_version != 1 for item in observations):
        raise _incomplete_selection()
    if len({item.workspace_id for item in observations}) != 1 or len(
        {item.created_by_run for item in observations}
    ) != 1:
        raise _mixed_selection()
    if not allow_locked and any(item.is_locked for item in observations):
        raise _locked_selection()
    return observations


def _owned(item: WorkspaceNodeObservation) -> OwnedNodeRef:
    return OwnedNodeRef(
        node_id=item.node_id,  # type: ignore[arg-type]
        path=item.path,
        node_type=item.node_type,
        parent_path=item.parent_path,
        capability=item.capability,  # type: ignore[arg-type]
        role=item.role,  # type: ignore[arg-type]
    )


def _summary(
    manifest: WorkspaceManifest, state: WorkspaceStateRecord | None
) -> WorkspaceSummary:
    active = state is not None and state.active_workspace_id == manifest.workspace_id
    return WorkspaceSummary(
        workspace_id=manifest.workspace_id,
        revision=manifest.revision,
        node_count=len(manifest.nodes),
        instance_id=manifest.instance_id,
        scene_epoch=manifest.scene_epoch,
        active=active,
    )


def _lifecycle(result: WorkspaceMutationResult) -> WorkspaceLifecycleSummary:
    state = result.state
    return WorkspaceLifecycleSummary(
        workspace=_summary(result.manifest, state),
        active_workspace_id=None if state is None else state.active_workspace_id,
        state_revision=None if state is None else state.state_revision,
        changed=result.changed,
    )


def _live_health(
    manifest: WorkspaceManifest, result: WorkspaceInspectResult
) -> WorkspaceHealth:
    if type(result) is not WorkspaceInspectResult:
        raise TypeError("provider must return an exact WorkspaceInspectResult")
    if result.mode != "manifest":
        raise ValueError("manifest inspection returned the wrong mode")
    observations = result.observations
    for item in observations:
        fields = (
            item.workspace_id,
            item.node_id,
            item.capability,
            item.role,
            item.schema_version,
            item.created_by_run,
        )
        if any(value is None for value in fields) or item.schema_version != 1:
            return WorkspaceHealth.CONFLICT
        if (
            item.workspace_id != manifest.workspace_id
            or item.created_by_run != manifest.created_by_run
        ):
            return WorkspaceHealth.CONFLICT
    by_id = {item.node_id: item for item in observations}
    stored = {item.node_id: item for item in manifest.nodes}
    if set(by_id) != set(stored):
        return WorkspaceHealth.STALE
    for node_id, expected in stored.items():
        live = by_id[node_id]
        if live.capability != expected.capability or live.role != expected.role:
            return WorkspaceHealth.CONFLICT
        if (
            live.path != expected.path
            or live.parent_path != expected.parent_path
            or live.node_type != expected.node_type
        ):
            return WorkspaceHealth.STALE
    if (
        result.binding.instance_id != manifest.instance_id
        or result.binding.scene_epoch != manifest.scene_epoch
    ):
        return WorkspaceHealth.STALE
    live_nodes = tuple(_owned(by_id[node.node_id]) for node in manifest.nodes)
    live_by_id = {node.node_id: node for node in live_nodes}
    live_roots = tuple(live_by_id[root.node_id] for root in manifest.roots)
    live_manifest = WorkspaceManifest.build(
        workspace_id=manifest.workspace_id,
        session_id=manifest.session_id,
        instance_id=result.binding.instance_id,
        scene_epoch=result.binding.scene_epoch,
        roots=live_roots,
        nodes=live_nodes,
        created_by_run=manifest.created_by_run,
        updated_at=manifest.updated_at,
    )
    if live_manifest.revision != manifest.revision:
        return WorkspaceHealth.STALE
    return WorkspaceHealth.HEALTHY


class WorkspaceService:
    def __init__(
        self,
        repository: ChangeSetRepository,
        *,
        provider: WorkspaceFactProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(repository) is not ChangeSetRepository:
            raise TypeError("repository must be an exact ChangeSetRepository")
        if provider is None:
            raise TypeError("provider is required")
        self._repository = repository
        self._provider = provider
        self._clock = _now_utc if clock is None else clock

    async def create(
        self, session_id: str, *, expected_scene_epoch: int | None
    ) -> WorkspaceLifecycleSummary:
        sid = _require_id_value(session_id, IdKind.SESSION, "session_id")
        epoch = _require_epoch(expected_scene_epoch)
        await self._repository.list_workspace_state(sid)
        try:
            result = await self._provider.inspect_selection(epoch)
        except WorkspaceInspectionUnavailable as exc:
            raise _bridge_unavailable() from exc
        except WorkspaceInspectionConflict as exc:
            raise _identity_conflict() from exc
        observations = _complete_selection(result, allow_locked=False)
        nodes = tuple(_owned(item) for item in observations)
        manifest = WorkspaceManifest.build(
            workspace_id=observations[0].workspace_id,  # type: ignore[arg-type]
            session_id=sid,
            instance_id=result.binding.instance_id,
            scene_epoch=result.binding.scene_epoch,
            roots=nodes,
            nodes=nodes,
            created_by_run=observations[0].created_by_run,  # type: ignore[arg-type]
            updated_at=_require_utc(self._clock()),
        )
        return _lifecycle(await self._repository.create_workspace_lifecycle(manifest))

    async def bind(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_manifest_revision: str,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        sid = _require_id_value(session_id, IdKind.SESSION, "session_id")
        wid = _require_id_value(workspace_id, IdKind.WORKSPACE, "workspace_id")
        expected = _require_revision(expected_manifest_revision)
        epoch = _require_epoch(expected_scene_epoch)
        stored = await _get_workspace_public(self._repository, wid)
        if stored.session_id != sid:
            raise _session_mismatch()
        if stored.revision != expected:
            raise _revision_conflict()
        try:
            result = await self._provider.inspect_selection(epoch)
        except WorkspaceInspectionUnavailable as exc:
            raise _bridge_unavailable() from exc
        except WorkspaceInspectionConflict as exc:
            raise _identity_conflict() from exc
        observations = _complete_selection(result, allow_locked=True)
        by_id = {item.node_id: item for item in observations}
        if set(by_id) != {node.node_id for node in stored.nodes}:
            raise _identity_conflict()
        for node in stored.nodes:
            live = by_id[node.node_id]
            if (
                live.workspace_id != stored.workspace_id
                or live.schema_version != 1
                or live.created_by_run != stored.created_by_run
                or live.node_type != node.node_type
                or live.capability != node.capability
                or live.role != node.role
            ):
                raise _identity_conflict()
        nodes = tuple(_owned(by_id[node.node_id]) for node in stored.nodes)
        nodes_by_id = {node.node_id: node for node in nodes}
        roots = tuple(nodes_by_id[root.node_id] for root in stored.roots)
        refreshed = WorkspaceManifest.build(
            workspace_id=stored.workspace_id,
            session_id=stored.session_id,
            instance_id=result.binding.instance_id,
            scene_epoch=result.binding.scene_epoch,
            roots=roots,
            nodes=nodes,
            created_by_run=stored.created_by_run,
            updated_at=_require_utc(self._clock()),
        )
        return _lifecycle(
            await self._repository.bind_workspace(
                refreshed, expected_manifest_revision=expected
            )
        )

    async def switch(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_active_workspace_id: str | None,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        sid = _require_id_value(session_id, IdKind.SESSION, "session_id")
        wid = _require_id_value(workspace_id, IdKind.WORKSPACE, "workspace_id")
        expected_active = (
            None
            if expected_active_workspace_id is None
            else _require_id_value(
                expected_active_workspace_id,
                IdKind.WORKSPACE,
                "expected_active_workspace_id",
            )
        )
        epoch = _require_epoch(expected_scene_epoch)
        manifest = await _get_workspace_public(self._repository, wid)
        if manifest.session_id != sid:
            raise _session_mismatch()
        active = await self._repository.get_active_workspace(sid)
        current_active = None if active is None else active.active_workspace_id
        if current_active != expected_active:
            raise _active_conflict()
        try:
            result = await self._provider.inspect_manifest(manifest, epoch)
        except WorkspaceInspectionUnavailable as exc:
            raise _bridge_unavailable() from exc
        except WorkspaceInspectionConflict as exc:
            raise _identity_conflict() from exc
        health = _live_health(manifest, result)
        if health is WorkspaceHealth.CONFLICT:
            raise _identity_conflict()
        if health is not WorkspaceHealth.HEALTHY:
            raise _revision_conflict()
        return _lifecycle(
            await self._repository.switch_workspace(
                sid,
                wid,
                expected_active_workspace_id=expected_active,
                expected_manifest_revision=manifest.revision,
                updated_at=_require_utc(self._clock()),
            )
        )

    async def inspect(
        self,
        session_id: str,
        workspace_id: str | None,
        *,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectionSummary:
        sid = _require_id_value(session_id, IdKind.SESSION, "session_id")
        wid = (
            None
            if workspace_id is None
            else _require_id_value(workspace_id, IdKind.WORKSPACE, "workspace_id")
        )
        epoch = _require_epoch(expected_scene_epoch)
        manifests, state = await self._repository.list_workspace_state(sid)
        active_id = None if state is None else state.active_workspace_id
        target_id = active_id if wid is None else wid
        if target_id is None:
            raise _workspace_not_found()
        by_id = {manifest.workspace_id: manifest for manifest in manifests}
        target = by_id.get(target_id)
        if target is None:
            raise _workspace_not_found()
        try:
            result = await self._provider.inspect_manifest(target, epoch)
        except WorkspaceInspectionUnavailable:
            status = WorkspaceHealth.BRIDGE_UNAVAILABLE
        except WorkspaceInspectionConflict:
            status = WorkspaceHealth.CONFLICT
        else:
            status = _live_health(target, result)
        summaries = tuple(_summary(manifest, state) for manifest in manifests)
        return WorkspaceInspectionSummary(summaries, active_id, target, status)


__all__ = [
    "WorkspaceFactProvider",
    "WorkspaceHealth",
    "WorkspaceInspectionSummary",
    "WorkspaceLifecycleSummary",
    "WorkspaceService",
    "WorkspaceSummary",
]
