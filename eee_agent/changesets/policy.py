"""Deterministic pure ChangeSet policy engine (Task 16-A).

:func:`evaluate_policy` consumes only immutable :class:`ChangeSet`,
:class:`WorkspaceManifest`, and explicit read facts. It is pure and
deterministic: it never mutates its inputs, performs no I/O, and repeated
evaluation produces equal decisions. It imports neither ``hou`` nor the legacy
bridge.

Every decision echoes the exact ChangeSet digest and carries
``approval_required=True``; Task 16 grants no standing permission and performs
no auto-approval. ``backup_required`` is derived from the typed risk facts, and
because no backup capability exists in this milestone a required backup denies.
"""

from __future__ import annotations

from collections.abc import Iterable

from eee_agent.changesets.contracts import (
    ChangeSet,
    ConnectInput,
    CreateNode,
    Effect,
    NodeRef,
    PermissionMode,
    PolicyDecision,
    SetParm,
    WorkspaceManifest,
)

# Stable sorted namespaced denial codes.
_MISSING_WORKSPACE = "policy.missing_workspace"
_STALE_WORKSPACE = "policy.stale_workspace"
_OWNERSHIP_MISMATCH = "policy.ownership_mismatch"
_OWNERSHIP_AMBIGUOUS = "policy.ownership_ambiguous"
_SCOPE_VIOLATION = "policy.scope_violation"
_LOCKED_TARGET = "policy.locked_target"
_AMBIGUOUS_TARGET = "policy.ambiguous_target"
_AFFECTED_TARGET_OMITTED = "policy.affected_target_omitted"
_EFFECT_CONTRADICTION = "policy.effect_contradiction"
_BACKUP_UNAVAILABLE = "policy.backup_unavailable"


def _normalize_path_set(value: object, label: str) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError(f"{label} must be a non-string iterable")
    out: set[str] = set()
    for item in value:
        if type(item) is not str:
            raise TypeError(f"{label} must contain only exact strings")
        out.add(item)
    return frozenset(out)


def _node_identity(ref: NodeRef) -> str:
    return ref.node_id if ref.node_id is not None else ref.path


def evaluate_policy(
    changeset: ChangeSet,
    *,
    workspace: WorkspaceManifest | None,
    locked_node_paths: Iterable[str] = (),
    ambiguous_node_paths: Iterable[str] = (),
) -> PolicyDecision:
    """Evaluate a ChangeSet against ownership/scope/effect policy.

    Returns an immutable :class:`PolicyDecision`. Malformed contract inputs
    (non-ChangeSet, non-manifest workspace, non-string path iterables) raise
    ``TypeError``/``ValueError``; returning denial reasons is the normal,
    non-exceptional path.
    """
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if workspace is not None and type(workspace) is not WorkspaceManifest:
        raise TypeError("workspace must be an exact WorkspaceManifest or None")
    locked = _normalize_path_set(locked_node_paths, "locked_node_paths")
    ambiguous = _normalize_path_set(ambiguous_node_paths, "ambiguous_node_paths")

    operations = changeset.operations
    derived_effects = tuple(sorted({op.effect.value for op in operations}))
    denial_codes: set[str] = set()

    risk = changeset.risk_summary
    if tuple(risk.effect_names) != derived_effects:
        denial_codes.add(_EFFECT_CONTRADICTION)
    if risk.changes_wiring != (Effect.WIRE_CONNECT.value in derived_effects):
        denial_codes.add(_EFFECT_CONTRADICTION)
    if risk.operation_count != len(operations):
        denial_codes.add(_EFFECT_CONTRADICTION)

    backup_required = bool(risk.requires_backup)
    if backup_required:
        denial_codes.add(_BACKUP_UNAVAILABLE)

    changed_targets: list[NodeRef] = []
    wire_sources: list[NodeRef] = []
    created_nodes: list[tuple[str, str]] = []
    has_create = False
    for op in operations:
        if isinstance(op, SetParm):
            changed_targets.append(op.target)
        elif isinstance(op, ConnectInput):
            changed_targets.append(op.target)
            wire_sources.append(op.source)
        elif isinstance(op, CreateNode):
            has_create = True
            created_nodes.append((op.node_id, op.workspace_id))

    for target in changed_targets:
        if target.path in locked:
            denial_codes.add(_LOCKED_TARGET)
        if target.path in ambiguous:
            denial_codes.add(_AMBIGUOUS_TARGET)

    mode = changeset.required_permission
    if mode is PermissionMode.OWNED_WORKSPACE:
        _evaluate_owned(
            changeset, workspace, changed_targets, wire_sources, created_nodes, denial_codes
        )
    elif mode is PermissionMode.SCOPED_PATCH:
        _evaluate_scoped(changeset, has_create, changed_targets, wire_sources, denial_codes)
    elif mode is PermissionMode.PROJECT_CHANGE:
        _evaluate_project(changeset, changed_targets, wire_sources, created_nodes, denial_codes)

    return PolicyDecision(
        allowed=not denial_codes,
        mode=mode,
        normalized_effects=derived_effects,
        approval_required=True,
        backup_required=backup_required,
        denial_codes=tuple(sorted(denial_codes)),
        changeset_digest=changeset.digest,
    )


def _evaluate_owned(
    changeset: ChangeSet,
    workspace: WorkspaceManifest | None,
    changed_targets: list[NodeRef],
    wire_sources: list[NodeRef],
    created_nodes: list[tuple[str, str]],
    denial_codes: set[str],
) -> None:
    if workspace is None:
        denial_codes.add(_MISSING_WORKSPACE)
        return
    if changeset.workspace_id is None or changeset.workspace_id != workspace.workspace_id:
        denial_codes.add(_OWNERSHIP_MISMATCH)

    binding = changeset.scene_binding
    if workspace.instance_id != binding.instance_id or workspace.scene_epoch != binding.scene_epoch:
        denial_codes.add(_STALE_WORKSPACE)

    owned_ids = {node.node_id for node in workspace.nodes}
    read_dep_ids = {_node_identity(ref) for ref in changeset.read_dependencies}

    for target in changed_targets:
        if target.node_id is None:
            denial_codes.add(_OWNERSHIP_AMBIGUOUS)
        elif target.node_id not in owned_ids:
            denial_codes.add(_OWNERSHIP_MISMATCH)

    # External nodes may appear only as read dependencies or unchanged create parents.
    for source in wire_sources:
        identity = _node_identity(source)
        if identity not in owned_ids and identity not in read_dep_ids:
            denial_codes.add(_OWNERSHIP_MISMATCH)

    for _node_id, workspace_id in created_nodes:
        if workspace_id != workspace.workspace_id:
            denial_codes.add(_OWNERSHIP_MISMATCH)


def _evaluate_scoped(
    changeset: ChangeSet,
    has_create: bool,
    changed_targets: list[NodeRef],
    wire_sources: list[NodeRef],
    denial_codes: set[str],
) -> None:
    if has_create:
        denial_codes.add(_SCOPE_VIOLATION)
    scoped_ids = set(changeset.scoped_node_ids)
    # Scope never expands via affected/read dependencies.
    for target in changed_targets:
        if target.node_id is None or target.node_id not in scoped_ids:
            denial_codes.add(_SCOPE_VIOLATION)
    for source in wire_sources:
        if source.node_id is None or source.node_id not in scoped_ids:
            denial_codes.add(_SCOPE_VIOLATION)


def _evaluate_project(
    changeset: ChangeSet,
    changed_targets: list[NodeRef],
    wire_sources: list[NodeRef],
    created_nodes: list[tuple[str, str]],
    denial_codes: set[str],
) -> None:
    affected_ids = {
        ref.node_id for ref in changeset.affected_nodes if ref.node_id is not None
    }
    affected_paths = {ref.path for ref in changeset.affected_nodes}

    def in_affected(ref: NodeRef) -> bool:
        if ref.node_id is not None:
            return ref.node_id in affected_ids
        return ref.path in affected_paths

    for target in changed_targets:
        if not in_affected(target):
            denial_codes.add(_AFFECTED_TARGET_OMITTED)
    for source in wire_sources:
        if not in_affected(source):
            denial_codes.add(_AFFECTED_TARGET_OMITTED)
    for node_id, _workspace_id in created_nodes:
        if node_id not in affected_ids:
            denial_codes.add(_AFFECTED_TARGET_OMITTED)
