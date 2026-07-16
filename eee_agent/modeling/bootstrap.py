"""Derive the first durable Workspace from a reconciled bootstrap receipt."""

from __future__ import annotations

from eee_agent.changesets.contracts import (
    ChangeReceipt,
    ChangeSet,
    CreateNode,
    OwnedNodeRef,
    PermissionMode,
    WorkspaceManifest,
)


class BootstrapFinalizeError(ValueError):
    """Bounded failure while converting an applied bootstrap into ownership."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> BootstrapFinalizeError:
    return BootstrapFinalizeError(code, message)


def derive_bootstrap_manifest(
    changeset: ChangeSet,
    receipt: ChangeReceipt,
) -> WorkspaceManifest:
    """Return exact owned facts only for a fully applied bootstrap ChangeSet."""
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(receipt) is not ChangeReceipt:
        raise TypeError("receipt must be an exact ChangeReceipt")
    if (
        changeset.workspace_id is not None
        or changeset.required_permission is not PermissionMode.PROJECT_CHANGE
    ):
        raise _fail(
            "modeling.bootstrap_changeset_invalid",
            "The ChangeSet is not an empty-scene bootstrap.",
        )
    if not receipt.is_success:
        raise _fail(
            "modeling.bootstrap_receipt_not_applied",
            "A Workspace cannot be created from an unsuccessful receipt.",
        )
    binding = changeset.scene_binding
    if (
        receipt.change_id != changeset.change_id
        or receipt.instance_id != binding.instance_id
        or receipt.scene_epoch != binding.scene_epoch
    ):
        raise _fail(
            "modeling.bootstrap_receipt_mismatch",
            "The bootstrap receipt does not match the proposed scene binding.",
        )
    operation_ids = tuple(operation.op_id for operation in changeset.operations)
    if set(receipt.applied_op_ids) != set(operation_ids) or len(
        receipt.applied_op_ids
    ) != len(operation_ids):
        raise _fail(
            "modeling.bootstrap_receipt_incomplete",
            "The bootstrap receipt does not cover every typed operation.",
        )

    creates = tuple(
        operation
        for operation in changeset.operations
        if isinstance(operation, CreateNode)
    )
    if not creates:
        raise _fail(
            "modeling.bootstrap_graph_invalid",
            "The bootstrap does not create an owned graph.",
        )
    workspace_ids = {operation.workspace_id for operation in creates}
    if len(workspace_ids) != 1:
        raise _fail(
            "modeling.bootstrap_graph_invalid",
            "Bootstrap nodes do not share one Workspace identity.",
        )
    workspace_id = next(iter(workspace_ids))
    root_ops = tuple(
        operation
        for operation in creates
        if operation.parent.path == "/obj"
        and operation.parent.node_id is None
        and operation.parent.expected_workspace_id is None
        and operation.node_type == "geo"
        and operation.role == "root"
    )
    if len(root_ops) != 1 or creates[0] is not root_ops[0]:
        raise _fail(
            "modeling.bootstrap_graph_invalid",
            "The bootstrap must begin with exactly one owned geo root.",
        )

    created_ids: set[str] = set()
    nodes: list[OwnedNodeRef] = []
    for operation in creates:
        if operation.workspace_id != workspace_id:
            raise _fail(
                "modeling.bootstrap_graph_invalid",
                "Bootstrap ownership changed inside the graph.",
            )
        if operation is not root_ops[0]:
            parent_id = operation.parent.node_id
            if (
                parent_id is None
                or parent_id not in created_ids
                or operation.parent.expected_workspace_id != workspace_id
            ):
                raise _fail(
                    "modeling.bootstrap_graph_invalid",
                    "Every non-root bootstrap node must have an earlier owned parent.",
                )
        path = f"{operation.parent.path.rstrip('/')}/{operation.node_name}"
        nodes.append(
            OwnedNodeRef(
                node_id=operation.node_id,
                path=path,
                node_type=operation.node_type,
                parent_path=operation.parent.path,
                capability=operation.capability,
                role=operation.role,
            )
        )
        created_ids.add(operation.node_id)

    root = nodes[0]
    return WorkspaceManifest.build(
        workspace_id=workspace_id,
        session_id=changeset.session_id,
        instance_id=receipt.instance_id,
        scene_epoch=receipt.scene_epoch,
        roots=(root,),
        nodes=tuple(nodes),
        created_by_run=changeset.run_id,
        updated_at=receipt.completed_at,
    )


__all__ = [
    "BootstrapFinalizeError",
    "derive_bootstrap_manifest",
]

