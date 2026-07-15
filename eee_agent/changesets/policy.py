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

Policy derives every effect, changed target, affected path, and external-touch
fact from the typed operations plus the workspace manifest/read facts, then
rejects any :class:`RiskSummary` that under- or over-reports them. For every
permission mode it also requires each changed target to be enumerated in
``affected_nodes``. OwnedWorkspace additionally requires changed targets and
owned wire sources to match the manifest's exact node identity (path, expected
type, and workspace) — ownership is never inferred from ``node_id`` alone.
"""

from __future__ import annotations

from collections.abc import Iterable

from eee_agent.changesets.contracts import (
    ChangeSet,
    ConnectInput,
    CreateNode,
    Effect,
    NodeRef,
    OwnedNodeRef,
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


def _derive_create_path(parent_path: str, node_name: str) -> str:
    return f"{parent_path.rstrip('/')}/{node_name}"


def _matches_manifest(ref: NodeRef, owned: OwnedNodeRef, workspace_id: str) -> bool:
    """Exact path/type/workspace match against a manifest owned-node fact."""
    return (
        ref.path == owned.path
        and ref.expected_type == owned.node_type
        and ref.expected_workspace_id == workspace_id
    )


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

    # Derive changed targets, wire sources, created nodes, and create paths.
    changed_targets: list[NodeRef] = []
    wire_sources: list[NodeRef] = []
    created_nodes: list[tuple[str, str]] = []
    create_paths: list[str] = []
    create_parents: list[NodeRef] = []
    for op in operations:
        if isinstance(op, SetParm):
            changed_targets.append(op.target)
        elif isinstance(op, ConnectInput):
            changed_targets.append(op.target)
            wire_sources.append(op.source)
        elif isinstance(op, CreateNode):
            created_nodes.append((op.node_id, op.workspace_id))
            create_paths.append(_derive_create_path(op.parent.path, op.node_name))
            create_parents.append(op.parent)

    for target in changed_targets:
        if target.path in locked:
            denial_codes.add(_LOCKED_TARGET)
        if target.path in ambiguous:
            denial_codes.add(_AMBIGUOUS_TARGET)

    # F3 (all modes): every changed target must be enumerated in affected_nodes.
    affected_identities = {_node_identity(ref) for ref in changeset.affected_nodes}
    for target in changed_targets:
        if _node_identity(target) not in affected_identities:
            denial_codes.add(_AFFECTED_TARGET_OMITTED)
    for source in wire_sources:
        if _node_identity(source) not in affected_identities:
            denial_codes.add(_AFFECTED_TARGET_OMITTED)
    for node_id, _workspace_id in created_nodes:
        if node_id not in affected_identities:
            denial_codes.add(_AFFECTED_TARGET_OMITTED)

    # F4: affected_paths must exactly equal the paths derived from the operations.
    derived_paths = {target.path for target in changed_targets}
    derived_paths |= {source.path for source in wire_sources}
    derived_paths |= set(create_paths)
    if set(risk.affected_paths) != derived_paths:
        denial_codes.add(_EFFECT_CONTRADICTION)

    # F4: touches_external_nodes must match the derived external-touch fact.
    created_ids = {node_id for node_id, _workspace_id in created_nodes}
    owned_ids = frozenset(node.node_id for node in workspace.nodes) if workspace else frozenset()
    owned_paths = frozenset(node.path for node in workspace.nodes) if workspace else frozenset()
    referenced = [*wire_sources, *create_parents]
    derived_touches_external = any(
        _is_external_reference(ref, workspace, created_ids, owned_ids, owned_paths, changeset.workspace_id)
        for ref in referenced
    )
    if risk.touches_external_nodes != derived_touches_external:
        denial_codes.add(_EFFECT_CONTRADICTION)

    mode = changeset.required_permission
    if mode is PermissionMode.OWNED_WORKSPACE:
        _evaluate_owned(
            changeset, workspace, changed_targets, wire_sources, created_nodes, denial_codes
        )
    elif mode is PermissionMode.SCOPED_PATCH:
        _evaluate_scoped(changeset, bool(created_nodes), changed_targets, wire_sources, denial_codes)
    # ProjectChange: no mode-specific rule beyond the global affected/effect checks.

    return PolicyDecision(
        allowed=not denial_codes,
        mode=mode,
        normalized_effects=derived_effects,
        approval_required=True,
        backup_required=backup_required,
        denial_codes=tuple(sorted(denial_codes)),
        changeset_digest=changeset.digest,
    )


def _is_external_reference(
    ref: NodeRef,
    workspace: WorkspaceManifest | None,
    created_ids: set[str],
    owned_ids: frozenset[str],
    owned_paths: frozenset[str],
    changeset_workspace_id: str | None,
) -> bool:
    """Whether a referenced wire source / create parent is external to the workspace."""
    if ref.node_id is not None and ref.node_id in created_ids:
        return False
    if workspace is not None:
        if ref.node_id is not None:
            return ref.node_id not in owned_ids
        return ref.path not in owned_paths
    # No manifest available: fall back to the node's declared workspace membership.
    if changeset_workspace_id is None:
        return ref.expected_workspace_id is not None
    return ref.expected_workspace_id is None or ref.expected_workspace_id != changeset_workspace_id


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

    owned_by_id = {node.node_id: node for node in workspace.nodes}
    owned_by_path = {node.path: node for node in workspace.nodes}
    read_dep_identities = {_node_identity(ref) for ref in changeset.read_dependencies}

    for target in changed_targets:
        if target.node_id is None:
            denial_codes.add(_OWNERSHIP_AMBIGUOUS)
            continue
        owned = owned_by_id.get(target.node_id)
        if owned is None:
            denial_codes.add(_OWNERSHIP_MISMATCH)
        elif not _matches_manifest(target, owned, workspace.workspace_id):
            denial_codes.add(_OWNERSHIP_MISMATCH)

    # External nodes may appear only as read dependencies or unchanged create parents.
    for source in wire_sources:
        if source.node_id is not None:
            owned = owned_by_id.get(source.node_id)
        else:
            owned = owned_by_path.get(source.path)
        if owned is None:
            if _node_identity(source) not in read_dep_identities:
                denial_codes.add(_OWNERSHIP_MISMATCH)
        elif not _matches_manifest(source, owned, workspace.workspace_id):
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
