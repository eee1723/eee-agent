"""Deterministic, catalog-gated ProceduralSpec to typed ChangeSet compiler."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from eee_agent.changesets.contracts import (
    ChangeSet,
    CheckpointPlan,
    ConnectInput,
    CreateNode,
    NodeAbsent,
    NodeIdentityEquals,
    NodeRef,
    ParmValueEquals,
    PermissionMode,
    RiskSummary,
    SceneBindingEquals,
    SetParm,
    WireInputEquals,
    WireRef,
    WorkspaceManifest,
    WorkspaceRevisionEquals,
)
from eee_agent.changesets.policy import evaluate_policy
from eee_agent.core.ids import IdKind, require_id
from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.modeling.contracts import (
    ModelingBrief,
    NodeSpec,
    ParmAssignment,
    ProceduralSpec,
    QualityProfile,
    parameter_value_shape,
)
from eee_agent.runtime.models import canonical_json_dumps

_MAX_CHANGESET_OPERATIONS = 256
_MAX_CHANGESET_CONDITIONS = 256


class ModelingCompileError(ValueError):
    """Bounded deterministic compiler failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _error(code: str, message: str) -> ModelingCompileError:
    return ModelingCompileError(code, message)


@dataclass(frozen=True, slots=True)
class ParmDefinition:
    parm_name: str
    default_value: object

    def __post_init__(self) -> None:
        normalized = ParmAssignment(self.parm_name, self.default_value)
        object.__setattr__(self, "parm_name", normalized.parm_name)
        object.__setattr__(self, "default_value", normalized.value)

    def to_dict(self) -> dict[str, object]:
        value = (
            list(self.default_value)
            if type(self.default_value) is tuple
            else self.default_value
        )
        return {"parm_name": self.parm_name, "default_value": value}


@dataclass(frozen=True, slots=True)
class NodeTypeDefinition:
    node_type: str
    parameters: tuple[ParmDefinition, ...]
    max_inputs: int
    max_output_index: int
    can_parent_nodes: bool = False

    def __post_init__(self) -> None:
        # Reuse the model-facing identifier validation without accepting a
        # model-owned default.
        probe = NodeSpec(
            node_key="catalog_probe",
            node_type=self.node_type,
            node_name="catalog_probe",
            parent_node=None,
            parameters=(),
            inputs=(),
        )
        object.__setattr__(self, "node_type", probe.node_type)
        if isinstance(self.parameters, str):
            raise TypeError("NodeTypeDefinition.parameters must be a sequence")
        items = tuple(self.parameters)
        seen: set[str] = set()
        for item in items:
            if type(item) is not ParmDefinition:
                raise TypeError(
                    "NodeTypeDefinition.parameters must contain ParmDefinition"
                )
            if item.parm_name in seen:
                raise ValueError(
                    "NodeTypeDefinition.parameters contains duplicate names"
                )
            seen.add(item.parm_name)
        if type(self.max_inputs) is not int or self.max_inputs < 0:
            raise ValueError("NodeTypeDefinition.max_inputs must be non-negative")
        if self.max_inputs > 64:
            raise ValueError("NodeTypeDefinition.max_inputs exceeds 64")
        if (
            type(self.max_output_index) is not int
            or self.max_output_index < 0
            or self.max_output_index > 63
        ):
            raise ValueError(
                "NodeTypeDefinition.max_output_index must be in 0..63"
            )
        if type(self.can_parent_nodes) is not bool:
            raise TypeError("NodeTypeDefinition.can_parent_nodes must be a bool")
        object.__setattr__(self, "parameters", items)

    @property
    def parameters_by_name(self) -> dict[str, ParmDefinition]:
        return {item.parm_name: item for item in self.parameters}

    def to_dict(self) -> dict[str, object]:
        return {
            "node_type": self.node_type,
            "parameters": [item.to_dict() for item in self.parameters],
            "max_inputs": self.max_inputs,
            "max_output_index": self.max_output_index,
            "can_parent_nodes": self.can_parent_nodes,
        }


@dataclass(frozen=True, slots=True)
class NodeCatalog:
    entries: tuple[NodeTypeDefinition, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("NodeCatalog.schema_version must be 1")
        if isinstance(self.entries, str):
            raise TypeError("NodeCatalog.entries must be a sequence")
        entries = tuple(self.entries)
        if not entries:
            raise ValueError("NodeCatalog.entries must not be empty")
        if len(entries) > 256:
            raise ValueError("NodeCatalog.entries exceeds 256")
        seen: set[str] = set()
        for item in entries:
            if type(item) is not NodeTypeDefinition:
                raise TypeError(
                    "NodeCatalog.entries must contain NodeTypeDefinition"
                )
            if item.node_type in seen:
                raise ValueError("NodeCatalog.entries contains duplicate node types")
            seen.add(item.node_type)
        object.__setattr__(
            self, "entries", tuple(sorted(entries, key=lambda item: item.node_type))
        )

    @property
    def by_type(self) -> dict[str, NodeTypeDefinition]:
        return {item.node_type: item for item in self.entries}

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_dumps(self.to_dict()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "entries": [item.to_dict() for item in self.entries],
        }


@dataclass(frozen=True, slots=True)
class CompilationResult:
    spec_digest: str
    catalog_digest: str
    changeset: ChangeSet

    def __post_init__(self) -> None:
        if type(self.changeset) is not ChangeSet:
            raise TypeError("CompilationResult.changeset must be a ChangeSet")

    def to_dict(self) -> dict[str, object]:
        return {
            "spec_digest": self.spec_digest,
            "catalog_digest": self.catalog_digest,
            "changeset": self.changeset.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WorkspaceBootstrapContext:
    """Trusted facts for compiling the first owned graph in an empty scene."""

    workspace_id: str
    root_name: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("WorkspaceBootstrapContext.schema_version must be 1")
        require_id(self.workspace_id, IdKind.WORKSPACE)
        probe = NodeSpec(
            node_key="bootstrap_root",
            node_type="geo",
            node_name=self.root_name,
            parent_node=None,
            parameters=(),
            inputs=(),
        )
        object.__setattr__(self, "root_name", probe.node_name)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_dumps(self.to_dict()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "root_name": self.root_name,
        }


@dataclass(frozen=True, slots=True)
class _LogicalNode:
    qualified_key: str
    component_id: str
    spec: NodeSpec
    declaration_index: int


def _stable_suffix(spec_digest: str, category: str, logical_key: str) -> str:
    return hashlib.sha256(
        f"{spec_digest}:{category}:{logical_key}".encode("utf-8")
    ).hexdigest()[:24]


def _topological_nodes(spec: ProceduralSpec) -> tuple[_LogicalNode, ...]:
    logical: list[_LogicalNode] = []
    by_key: dict[str, _LogicalNode] = {}
    for component in spec.components:
        for node in component.nodes:
            qualified = f"{component.component_id}.{node.node_key}"
            item = _LogicalNode(
                qualified_key=qualified,
                component_id=component.component_id,
                spec=node,
                declaration_index=len(logical),
            )
            logical.append(item)
            by_key[qualified] = item

    dependencies: dict[str, set[str]] = {}
    for item in logical:
        refs = {
            binding.source_node for binding in item.spec.inputs
        }
        if item.spec.parent_node is not None:
            refs.add(item.spec.parent_node)
        refs.discard(item.qualified_key)
        dependencies[item.qualified_key] = refs
        if item.qualified_key in (
            {binding.source_node for binding in item.spec.inputs}
            | (
                {item.spec.parent_node}
                if item.spec.parent_node is not None
                else set()
            )
        ):
            raise _error(
                "modeling.dependency_cycle",
                f"Node {item.qualified_key!r} depends on itself.",
            )

    remaining = dict(dependencies)
    ordered: list[_LogicalNode] = []
    emitted: set[str] = set()
    while remaining:
        ready = [
            by_key[key]
            for key, deps in remaining.items()
            if deps.issubset(emitted)
        ]
        if not ready:
            raise _error(
                "modeling.dependency_cycle",
                "The ProceduralSpec node graph contains a dependency cycle.",
            )
        ready.sort(key=lambda item: item.declaration_index)
        for item in ready:
            ordered.append(item)
            emitted.add(item.qualified_key)
            remaining.pop(item.qualified_key)
    return tuple(ordered)


def _validate_catalog(
    ordered: tuple[_LogicalNode, ...],
    catalog: NodeCatalog,
) -> dict[str, NodeTypeDefinition]:
    definitions: dict[str, NodeTypeDefinition] = {}
    catalog_by_type = catalog.by_type
    logical_by_key = {item.qualified_key: item for item in ordered}
    for item in ordered:
        definition = catalog_by_type.get(item.spec.node_type)
        if definition is None:
            raise _error(
                "modeling.catalog_node_denied",
                f"Node type {item.spec.node_type!r} is not in the trusted catalog.",
            )
        definitions[item.qualified_key] = definition
        allowed_parms = definition.parameters_by_name
        for assignment in item.spec.parameters:
            parm = allowed_parms.get(assignment.parm_name)
            if parm is None:
                raise _error(
                    "modeling.catalog_parm_denied",
                    f"Parameter {assignment.parm_name!r} is not allowed for "
                    f"{item.spec.node_type!r}.",
                )
            if parameter_value_shape(assignment.value) != parameter_value_shape(
                parm.default_value
            ):
                raise _error(
                    "modeling.catalog_value_mismatch",
                    f"Parameter {assignment.parm_name!r} has the wrong value shape.",
                )
        if item.spec.parent_node is not None:
            parent_definition = definitions.get(item.spec.parent_node)
            if parent_definition is None:
                # Topological ordering guarantees this is an earlier logical
                # node; this branch is defensive.
                raise _error(
                    "modeling.dependency_cycle",
                    f"Parent {item.spec.parent_node!r} is not available.",
                )
            if not parent_definition.can_parent_nodes:
                raise _error(
                    "modeling.catalog_parent_denied",
                    f"Node {item.spec.parent_node!r} cannot contain child nodes.",
                )
        for binding in item.spec.inputs:
            if binding.input_index >= definition.max_inputs:
                raise _error(
                    "modeling.input_out_of_range",
                    f"Input {binding.input_index} is outside the target catalog bounds.",
                )
            source = logical_by_key[binding.source_node]
            source_definition = definitions.get(source.qualified_key)
            if source_definition is None:
                source_definition = catalog_by_type[source.spec.node_type]
            if binding.source_output_index > source_definition.max_output_index:
                raise _error(
                    "modeling.input_out_of_range",
                    f"Output {binding.source_output_index} is outside the source catalog bounds.",
                )
    return definitions


def compile_procedural_spec(
    *,
    brief: ModelingBrief,
    spec: ProceduralSpec,
    quality_profile: QualityProfile,
    catalog: NodeCatalog,
    workspace: WorkspaceManifest,
    scene_binding: SceneBinding,
    session_id: str,
    run_id: str,
    change_id: str,
    created_at: datetime,
) -> CompilationResult:
    """Compile strict modeling intent into an existing trusted ChangeSet."""
    if type(brief) is not ModelingBrief:
        raise TypeError("brief must be an exact ModelingBrief")
    if type(spec) is not ProceduralSpec:
        raise TypeError("spec must be an exact ProceduralSpec")
    if type(quality_profile) is not QualityProfile:
        raise TypeError("quality_profile must be an exact QualityProfile")
    if type(catalog) is not NodeCatalog:
        raise TypeError("catalog must be an exact NodeCatalog")
    if type(workspace) is not WorkspaceManifest:
        raise TypeError("workspace must be an exact WorkspaceManifest")
    if type(scene_binding) is not SceneBinding:
        raise TypeError("scene_binding must be an exact SceneBinding")

    if spec.brief_digest != brief.digest:
        raise _error(
            "modeling.brief_mismatch",
            "The ProceduralSpec does not bind the supplied ModelingBrief.",
        )
    if spec.quality_profile_id != quality_profile.profile_id:
        raise _error(
            "modeling.profile_mismatch",
            "The ProceduralSpec does not bind the supplied QualityProfile.",
        )
    if (
        workspace.session_id != session_id
        or workspace.instance_id != scene_binding.instance_id
        or workspace.scene_epoch != scene_binding.scene_epoch
    ):
        raise _error(
            "modeling.workspace_mismatch",
            "The Workspace, Session, and scene binding do not agree.",
        )
    roots = [
        root
        for root in workspace.roots
        if root.node_id == spec.workspace_root_node_id
    ]
    if len(roots) != 1:
        raise _error(
            "modeling.workspace_mismatch",
            "The requested Workspace root is not an exact manifest root.",
        )
    root = roots[0]

    ordered = _topological_nodes(spec)
    if len(ordered) > quality_profile.max_compiled_nodes:
        raise _error(
            "modeling.node_budget_exceeded",
            "The ProceduralSpec exceeds the QualityProfile node budget.",
        )
    definitions = _validate_catalog(ordered, catalog)
    spec_digest = spec.digest

    root_ref = NodeRef(
        node_id=root.node_id,
        path=root.path,
        expected_type=root.node_type,
        expected_workspace_id=workspace.workspace_id,
    )
    node_refs: dict[str, NodeRef] = {}
    create_ops: list[CreateNode] = []
    created_paths: set[str] = set()

    for item in ordered:
        if item.spec.parent_node is None:
            parent_ref = root_ref
        else:
            parent_ref = node_refs[item.spec.parent_node]
        path = f"{parent_ref.path.rstrip('/')}/{item.spec.node_name}"
        if path in created_paths:
            raise _error(
                "modeling.path_collision",
                f"Compiled node path {path!r} is duplicated.",
            )
        created_paths.add(path)
        node_id = f"n_{_stable_suffix(spec_digest, 'node', item.qualified_key)}"
        ref = NodeRef(
            node_id=node_id,
            path=path,
            expected_type=item.spec.node_type,
            expected_workspace_id=workspace.workspace_id,
        )
        node_refs[item.qualified_key] = ref
        create_ops.append(
            CreateNode(
                op_id=(
                    f"op_create_"
                    f"{_stable_suffix(spec_digest, 'create', item.qualified_key)}"
                ),
                parent=parent_ref,
                node_id=node_id,
                node_type=item.spec.node_type,
                node_name=item.spec.node_name,
                workspace_id=workspace.workspace_id,
                capability="modeling",
                role=next(
                    component.role
                    for component in spec.components
                    if component.component_id == item.component_id
                ),
            )
        )

    parm_ops: list[SetParm] = []
    wire_ops: list[ConnectInput] = []
    parm_postconditions: list[ParmValueEquals] = []
    wire_postconditions: list[WireInputEquals] = []
    for item in ordered:
        target = node_refs[item.qualified_key]
        defaults = definitions[item.qualified_key].parameters_by_name
        for assignment in item.spec.parameters:
            default = defaults[assignment.parm_name].default_value
            parm_ops.append(
                SetParm(
                    op_id=(
                        f"op_parm_"
                        f"{_stable_suffix(spec_digest, 'parm', item.qualified_key + '.' + assignment.parm_name)}"
                    ),
                    target=target,
                    parm_name=assignment.parm_name,
                    value=assignment.value,
                    expected_old_value=default,
                )
            )
            parm_postconditions.append(
                ParmValueEquals(
                    target=target,
                    parm_name=assignment.parm_name,
                    value=assignment.value,
                )
            )
        for binding in item.spec.inputs:
            source = node_refs[binding.source_node]
            wire_ops.append(
                ConnectInput(
                    op_id=(
                        f"op_wire_"
                        f"{_stable_suffix(spec_digest, 'wire', item.qualified_key + '.' + str(binding.input_index))}"
                    ),
                    target=target,
                    input_index=binding.input_index,
                    source=source,
                    source_output_index=binding.source_output_index,
                    expected_old_source=None,
                )
            )
            wire_postconditions.append(
                WireInputEquals(
                    target=target,
                    input_index=binding.input_index,
                    source=WireRef(
                        source=source,
                        source_output_index=binding.source_output_index,
                    ),
                )
            )

    operations = tuple([*create_ops, *parm_ops, *wire_ops])
    affected_nodes = tuple(node_refs[item.qualified_key] for item in ordered)
    if len(operations) > _MAX_CHANGESET_OPERATIONS:
        raise _error(
            "modeling.operation_budget_exceeded",
            "The compiled graph exceeds the typed ChangeSet operation budget.",
        )
    if len(affected_nodes) + 3 > _MAX_CHANGESET_CONDITIONS:
        raise _error(
            "modeling.condition_budget_exceeded",
            "The compiled graph exceeds the typed ChangeSet precondition budget.",
        )
    if (
        len(affected_nodes)
        + len(parm_postconditions)
        + len(wire_postconditions)
        > _MAX_CHANGESET_CONDITIONS
    ):
        raise _error(
            "modeling.condition_budget_exceeded",
            "The compiled graph exceeds the typed ChangeSet postcondition budget.",
        )
    preconditions = (
        SceneBindingEquals(
            instance_id=scene_binding.instance_id,
            scene_epoch=scene_binding.scene_epoch,
        ),
        WorkspaceRevisionEquals(
            workspace_id=workspace.workspace_id,
            revision=workspace.revision,
        ),
        NodeIdentityEquals(node=root_ref),
        *(
            NodeAbsent(path=ref.path, node_id=ref.node_id)
            for ref in affected_nodes
        ),
    )
    postconditions = (
        *(NodeIdentityEquals(node=ref) for ref in affected_nodes),
        *parm_postconditions,
        *wire_postconditions,
    )
    effect_names = tuple(sorted({operation.effect.value for operation in operations}))
    try:
        changeset = ChangeSet(
            change_id=change_id,
            session_id=session_id,
            run_id=run_id,
            scene_binding=scene_binding,
            workspace_id=workspace.workspace_id,
            base_revision=workspace.revision,
            required_permission=PermissionMode.OWNED_WORKSPACE,
            scoped_node_ids=(),
            operations=operations,
            affected_nodes=affected_nodes,
            read_dependencies=(),
            preconditions=preconditions,
            expected_postconditions=postconditions,
            risk_summary=RiskSummary(
                touches_external_nodes=False,
                changes_wiring=bool(wire_ops),
                requires_backup=False,
                operation_count=len(operations),
                effect_names=effect_names,
                affected_paths=tuple(ref.path for ref in affected_nodes),
            ),
            checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
            created_at=created_at,
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "modeling.changeset_invalid",
            "The compiled graph could not satisfy the typed ChangeSet contract.",
        ) from exc
    decision = evaluate_policy(changeset, workspace=workspace)
    if not decision.allowed:
        raise _error(
            "modeling.policy_denied",
            "The compiled ChangeSet was denied by the trusted policy engine.",
        )
    return CompilationResult(
        spec_digest=spec_digest,
        catalog_digest=catalog.digest,
        changeset=changeset,
    )


def compile_bootstrap_procedural_spec(
    *,
    brief: ModelingBrief,
    spec: ProceduralSpec,
    quality_profile: QualityProfile,
    catalog: NodeCatalog,
    bootstrap: WorkspaceBootstrapContext,
    scene_binding: SceneBinding,
    session_id: str,
    run_id: str,
    change_id: str,
    created_at: datetime,
) -> CompilationResult:
    """Compile the first owned graph without a provisional WorkspaceManifest."""
    if type(brief) is not ModelingBrief:
        raise TypeError("brief must be an exact ModelingBrief")
    if type(spec) is not ProceduralSpec:
        raise TypeError("spec must be an exact ProceduralSpec")
    if type(quality_profile) is not QualityProfile:
        raise TypeError("quality_profile must be an exact QualityProfile")
    if type(catalog) is not NodeCatalog:
        raise TypeError("catalog must be an exact NodeCatalog")
    if type(bootstrap) is not WorkspaceBootstrapContext:
        raise TypeError("bootstrap must be an exact WorkspaceBootstrapContext")
    if type(scene_binding) is not SceneBinding:
        raise TypeError("scene_binding must be an exact SceneBinding")
    if spec.brief_digest != brief.digest:
        raise _error(
            "modeling.brief_mismatch",
            "The ProceduralSpec does not bind the supplied ModelingBrief.",
        )
    if spec.quality_profile_id != quality_profile.profile_id:
        raise _error(
            "modeling.profile_mismatch",
            "The ProceduralSpec does not bind the supplied QualityProfile.",
        )
    if spec.workspace_root_node_id != "bootstrap_root":
        raise _error(
            "modeling.bootstrap_root_invalid",
            "A bootstrap ProceduralSpec must use the bootstrap_root sentinel.",
        )

    ordered = _topological_nodes(spec)
    if len(ordered) + 1 > quality_profile.max_compiled_nodes:
        raise _error(
            "modeling.node_budget_exceeded",
            "The bootstrap graph exceeds the QualityProfile node budget.",
        )
    definitions = _validate_catalog(ordered, catalog)
    root_definition = catalog.by_type.get("geo")
    if root_definition is None or not root_definition.can_parent_nodes:
        raise _error(
            "modeling.bootstrap_catalog_invalid",
            "The trusted catalog does not contain the verified geo root.",
        )

    spec_digest = spec.digest
    stable_seed = hashlib.sha256(
        f"{spec_digest}:{bootstrap.digest}".encode("utf-8")
    ).hexdigest()
    parent_ref = NodeRef(
        node_id=None,
        path="/obj",
        expected_type="obj",
        expected_workspace_id=None,
    )
    root_ref = NodeRef(
        node_id=f"n_{_stable_suffix(stable_seed, 'node', 'bootstrap_root')}",
        path=f"/obj/{bootstrap.root_name}",
        expected_type="geo",
        expected_workspace_id=bootstrap.workspace_id,
    )
    root_create = CreateNode(
        op_id=f"op_create_{_stable_suffix(stable_seed, 'create', 'bootstrap_root')}",
        parent=parent_ref,
        node_id=root_ref.node_id or "",
        node_type="geo",
        node_name=bootstrap.root_name,
        workspace_id=bootstrap.workspace_id,
        capability="modeling",
        role="root",
    )

    node_refs: dict[str, NodeRef] = {}
    create_ops: list[CreateNode] = [root_create]
    created_paths: set[str] = {root_ref.path}
    roles = {component.component_id: component.role for component in spec.components}
    for item in ordered:
        logical_parent = item.spec.parent_node
        node_parent = root_ref if logical_parent is None else node_refs[logical_parent]
        path = f"{node_parent.path.rstrip('/')}/{item.spec.node_name}"
        if path in created_paths:
            raise _error(
                "modeling.path_collision",
                f"Compiled node path {path!r} is duplicated.",
            )
        created_paths.add(path)
        node_id = f"n_{_stable_suffix(stable_seed, 'node', item.qualified_key)}"
        ref = NodeRef(
            node_id=node_id,
            path=path,
            expected_type=item.spec.node_type,
            expected_workspace_id=bootstrap.workspace_id,
        )
        node_refs[item.qualified_key] = ref
        create_ops.append(
            CreateNode(
                op_id=(
                    f"op_create_"
                    f"{_stable_suffix(stable_seed, 'create', item.qualified_key)}"
                ),
                parent=node_parent,
                node_id=node_id,
                node_type=item.spec.node_type,
                node_name=item.spec.node_name,
                workspace_id=bootstrap.workspace_id,
                capability="modeling",
                role=roles[item.component_id],
            )
        )

    parm_ops: list[SetParm] = []
    wire_ops: list[ConnectInput] = []
    parm_postconditions: list[ParmValueEquals] = []
    wire_postconditions: list[WireInputEquals] = []
    for item in ordered:
        target = node_refs[item.qualified_key]
        defaults = definitions[item.qualified_key].parameters_by_name
        for assignment in item.spec.parameters:
            default = defaults[assignment.parm_name].default_value
            parm_ops.append(
                SetParm(
                    op_id=(
                        f"op_parm_"
                        f"{_stable_suffix(stable_seed, 'parm', item.qualified_key + '.' + assignment.parm_name)}"
                    ),
                    target=target,
                    parm_name=assignment.parm_name,
                    value=assignment.value,
                    expected_old_value=default,
                )
            )
            parm_postconditions.append(
                ParmValueEquals(
                    target=target,
                    parm_name=assignment.parm_name,
                    value=assignment.value,
                )
            )
        for binding in item.spec.inputs:
            source = node_refs[binding.source_node]
            wire_ops.append(
                ConnectInput(
                    op_id=(
                        f"op_wire_"
                        f"{_stable_suffix(stable_seed, 'wire', item.qualified_key + '.' + str(binding.input_index))}"
                    ),
                    target=target,
                    input_index=binding.input_index,
                    source=source,
                    source_output_index=binding.source_output_index,
                    expected_old_source=None,
                )
            )
            wire_postconditions.append(
                WireInputEquals(
                    target=target,
                    input_index=binding.input_index,
                    source=WireRef(
                        source=source,
                        source_output_index=binding.source_output_index,
                    ),
                )
            )

    operations = tuple([*create_ops, *parm_ops, *wire_ops])
    affected_nodes = (root_ref,) + tuple(
        node_refs[item.qualified_key] for item in ordered
    )
    if len(operations) > _MAX_CHANGESET_OPERATIONS:
        raise _error(
            "modeling.operation_budget_exceeded",
            "The bootstrap graph exceeds the typed ChangeSet operation budget.",
        )
    if len(affected_nodes) + 2 > _MAX_CHANGESET_CONDITIONS:
        raise _error(
            "modeling.condition_budget_exceeded",
            "The bootstrap graph exceeds the typed ChangeSet precondition budget.",
        )
    if (
        len(affected_nodes)
        + len(parm_postconditions)
        + len(wire_postconditions)
        > _MAX_CHANGESET_CONDITIONS
    ):
        raise _error(
            "modeling.condition_budget_exceeded",
            "The bootstrap graph exceeds the typed ChangeSet postcondition budget.",
        )
    preconditions = (
        SceneBindingEquals(
            instance_id=scene_binding.instance_id,
            scene_epoch=scene_binding.scene_epoch,
        ),
        NodeIdentityEquals(node=parent_ref),
        *(NodeAbsent(path=ref.path, node_id=ref.node_id or "") for ref in affected_nodes),
    )
    postconditions = (
        *(NodeIdentityEquals(node=ref) for ref in affected_nodes),
        *parm_postconditions,
        *wire_postconditions,
    )
    effect_names = tuple(sorted({operation.effect.value for operation in operations}))
    try:
        changeset = ChangeSet(
            change_id=change_id,
            session_id=session_id,
            run_id=run_id,
            scene_binding=scene_binding,
            workspace_id=None,
            base_revision=scene_binding.observed_revision,
            required_permission=PermissionMode.PROJECT_CHANGE,
            scoped_node_ids=(),
            operations=operations,
            affected_nodes=affected_nodes,
            read_dependencies=(parent_ref,),
            preconditions=preconditions,
            expected_postconditions=postconditions,
            risk_summary=RiskSummary(
                touches_external_nodes=True,
                changes_wiring=bool(wire_ops),
                requires_backup=False,
                operation_count=len(operations),
                effect_names=effect_names,
                affected_paths=tuple(ref.path for ref in affected_nodes),
            ),
            checkpoint_plan=CheckpointPlan(nodes=(), parameters=(), wires=()),
            created_at=created_at,
        )
    except (TypeError, ValueError) as exc:
        raise _error(
            "modeling.changeset_invalid",
            "The bootstrap graph could not satisfy the typed ChangeSet contract.",
        ) from exc
    decision = evaluate_policy(changeset, workspace=None)
    if not decision.allowed:
        raise _error(
            "modeling.policy_denied",
            "The bootstrap ChangeSet was denied by the trusted policy engine.",
        )
    return CompilationResult(
        spec_digest=spec_digest,
        catalog_digest=catalog.digest,
        changeset=changeset,
    )


__all__ = [
    "CompilationResult",
    "ModelingCompileError",
    "NodeCatalog",
    "NodeTypeDefinition",
    "ParmDefinition",
    "WorkspaceBootstrapContext",
    "compile_bootstrap_procedural_spec",
    "compile_procedural_spec",
]
