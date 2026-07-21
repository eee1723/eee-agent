"""Strict, model-facing contracts for the Task 18 modeling capability.

The contracts contain intent and graph description only. They deliberately do
not expose ChangeSet operations, Houdini paths, expected-old facts, permission
modes, Bridge calls, or Apply authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from eee_agent.runtime.models import canonical_json_dumps

MAX_MODELING_JSON_BYTES = 256 * 1024

_MAX_IDENTIFIER = 128
_MAX_TITLE = 256
_MAX_TEXT = 4096
_MAX_CONSTRAINTS = 64
_MAX_COMPONENTS = 64
_MAX_NODES_PER_COMPONENT = 64
_MAX_PARMS_PER_NODE = 64
_MAX_INPUTS_PER_NODE = 64
_MAX_EVIDENCE = 16
_MAX_FAILED_SAMPLES = 32
_MAX_PARM_VALUE_BYTES = 16 * 1024

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_QUALIFIED_NODE_RE = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_CODE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f]")


class UnitSystem(StrEnum):
    MILLIMETERS = "Millimeters"
    CENTIMETERS = "Centimeters"
    METERS = "Meters"


class Axis(StrEnum):
    X = "X"
    Y = "Y"
    Z = "Z"


class FrontAxis(StrEnum):
    POSITIVE_X = "PositiveX"
    NEGATIVE_X = "NegativeX"
    POSITIVE_Y = "PositiveY"
    NEGATIVE_Y = "NegativeY"
    POSITIVE_Z = "PositiveZ"
    NEGATIVE_Z = "NegativeZ"

    @property
    def axis(self) -> Axis:
        return Axis(self.value[-1])


class ValidatorKind(StrEnum):
    SPEC_CONTRACT = "SpecContract"
    GRAPH = "Graph"
    COOK = "Cook"
    GEOMETRY = "Geometry"
    PARAMETER_SENSITIVITY = "ParameterSensitivity"
    SEMANTIC = "Semantic"
    ARTIFACT = "Artifact"


class RepairStatus(StrEnum):
    OPEN = "Open"
    RESOLVED = "Resolved"
    EXHAUSTED = "Exhausted"


def _require_identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a non-empty identifier")
    if len(value) > _MAX_IDENTIFIER:
        raise ValueError(f"{label} exceeds the maximum identifier length")
    return value


def _require_qualified_node(value: object, label: str) -> str:
    if type(value) is not str or _QUALIFIED_NODE_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a qualified component.node key")
    if len(value) > (2 * _MAX_IDENTIFIER + 1):
        raise ValueError(f"{label} exceeds the maximum qualified-key length")
    return value


def _require_text(value: object, label: str, maximum: int) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds the maximum length")
    if _CONTROL_RE.search(value) is not None:
        raise ValueError(f"{label} must not contain control characters")
    return value


def _require_exact_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


def _require_exact_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{label} must be a bool")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must contain exactly 64 lowercase hex characters")
    return value


def _require_exact_keys(
    value: object, fields: frozenset[str], label: str
) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    if set(value) != fields:
        raise ValueError(f"{label} must have exactly the required fields")
    return value


def _sequence(value: object, label: str, maximum: int) -> tuple[object, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence")
    items = tuple(value)
    if len(items) > maximum:
        raise ValueError(f"{label} exceeds the maximum count")
    return items


def _identifier_sequence(
    value: object, label: str, maximum: int
) -> tuple[str, ...]:
    items = _sequence(value, label, maximum)
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _require_identifier(item, label)
        if normalized in seen:
            raise ValueError(f"{label} contains duplicate identifiers")
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def _json_parm_value(value: object) -> object:
    if type(value) is tuple:
        return list(value)
    return value


def _validate_parm_value(value: object, label: str) -> object:
    if type(value) in (bool, int, str):
        normalized = value
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
        normalized = value
    elif type(value) in (list, tuple):
        items = tuple(value)
        if not items:
            raise ValueError(f"{label} tuple must not be empty")
        if len(items) > 16:
            raise ValueError(f"{label} tuple exceeds the maximum length")
        item_type = type(items[0])
        if item_type not in (bool, int, float, str):
            raise TypeError(f"{label} tuple entries must be JSON scalars")
        for item in items:
            if type(item) is not item_type:
                raise ValueError(f"{label} tuple must be homogeneous")
            if type(item) is float and not math.isfinite(item):
                raise ValueError(f"{label} tuple must contain finite floats")
        normalized = items
    else:
        raise TypeError(f"{label} must be a JSON scalar or homogeneous tuple")
    size = len(canonical_json_dumps(_json_parm_value(normalized)).encode("utf-8"))
    if size > _MAX_PARM_VALUE_BYTES:
        raise ValueError(f"{label} exceeds the maximum value size")
    return normalized


def parameter_value_shape(value: object) -> tuple[object, ...]:
    """Return the exact scalar/tuple type shape used by the trusted catalog."""
    if type(value) is tuple:
        return ("tuple", type(value[0]), len(value))
    return ("scalar", type(value))


@dataclass(frozen=True, slots=True)
class BriefConstraint:
    code: str
    statement: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or _CODE_RE.fullmatch(self.code) is None:
            raise ValueError("BriefConstraint.code must be a namespaced code")
        _require_text(self.statement, "BriefConstraint.statement", _MAX_TEXT)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "statement": self.statement}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BriefConstraint:
        data = _require_exact_keys(
            value, frozenset({"code", "statement"}), "BriefConstraint"
        )
        return cls(code=data["code"], statement=data["statement"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ModelingBrief:
    brief_key: str
    title: str
    asset_family: str
    goal: str
    units: UnitSystem
    up_axis: Axis
    front_axis: FrontAxis
    constraints: tuple[BriefConstraint, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ModelingBrief.schema_version must be 1")
        _require_identifier(self.brief_key, "ModelingBrief.brief_key")
        _require_text(self.title, "ModelingBrief.title", _MAX_TITLE)
        _require_identifier(self.asset_family, "ModelingBrief.asset_family")
        _require_text(self.goal, "ModelingBrief.goal", _MAX_TEXT)
        if type(self.units) is not UnitSystem:
            raise ValueError("ModelingBrief.units must be an exact UnitSystem")
        if type(self.up_axis) is not Axis:
            raise ValueError("ModelingBrief.up_axis must be an exact Axis")
        if type(self.front_axis) is not FrontAxis:
            raise ValueError("ModelingBrief.front_axis must be an exact FrontAxis")
        if self.front_axis.axis is self.up_axis:
            raise ValueError("ModelingBrief.front_axis must not parallel up_axis")
        items = _sequence(
            self.constraints, "ModelingBrief.constraints", _MAX_CONSTRAINTS
        )
        seen: set[str] = set()
        for item in items:
            if type(item) is not BriefConstraint:
                raise TypeError(
                    "ModelingBrief.constraints must contain BriefConstraint"
                )
            if item.code in seen:
                raise ValueError("ModelingBrief.constraints contains duplicate codes")
            seen.add(item.code)
        object.__setattr__(self, "constraints", items)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_dumps(self.to_dict()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "brief_key": self.brief_key,
            "title": self.title,
            "asset_family": self.asset_family,
            "goal": self.goal,
            "units": self.units.value,
            "up_axis": self.up_axis.value,
            "front_axis": self.front_axis.value,
            "constraints": [item.to_dict() for item in self.constraints],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelingBrief:
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "brief_key",
                    "title",
                    "asset_family",
                    "goal",
                    "units",
                    "up_axis",
                    "front_axis",
                    "constraints",
                }
            ),
            "ModelingBrief",
        )
        constraints = _sequence(
            data["constraints"], "ModelingBrief.constraints", _MAX_CONSTRAINTS
        )
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            brief_key=data["brief_key"],  # type: ignore[arg-type]
            title=data["title"],  # type: ignore[arg-type]
            asset_family=data["asset_family"],  # type: ignore[arg-type]
            goal=data["goal"],  # type: ignore[arg-type]
            units=UnitSystem(data["units"]),
            up_axis=Axis(data["up_axis"]),
            front_axis=FrontAxis(data["front_axis"]),
            constraints=tuple(BriefConstraint.from_dict(item) for item in constraints),
        )


@dataclass(frozen=True, slots=True)
class ParmAssignment:
    parm_name: str
    value: object

    def __post_init__(self) -> None:
        _require_identifier(self.parm_name, "ParmAssignment.parm_name")
        object.__setattr__(
            self, "value", _validate_parm_value(self.value, "ParmAssignment.value")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "parm_name": self.parm_name,
            "value": _json_parm_value(self.value),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ParmAssignment:
        data = _require_exact_keys(
            value, frozenset({"parm_name", "value"}), "ParmAssignment"
        )
        return cls(data["parm_name"], data["value"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class InputBinding:
    input_index: int
    source_node: str
    source_output_index: int

    def __post_init__(self) -> None:
        _require_exact_int(self.input_index, "InputBinding.input_index")
        if self.input_index < 0:
            raise ValueError("InputBinding.input_index must be non-negative")
        _require_qualified_node(self.source_node, "InputBinding.source_node")
        _require_exact_int(
            self.source_output_index, "InputBinding.source_output_index"
        )
        if self.source_output_index < 0:
            raise ValueError(
                "InputBinding.source_output_index must be non-negative"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "input_index": self.input_index,
            "source_node": self.source_node,
            "source_output_index": self.source_output_index,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InputBinding:
        data = _require_exact_keys(
            value,
            frozenset({"input_index", "source_node", "source_output_index"}),
            "InputBinding",
        )
        return cls(
            data["input_index"],  # type: ignore[arg-type]
            data["source_node"],  # type: ignore[arg-type]
            data["source_output_index"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class NodeSpec:
    node_key: str
    node_type: str
    node_name: str
    parent_node: str | None
    parameters: tuple[ParmAssignment, ...]
    inputs: tuple[InputBinding, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.node_key, "NodeSpec.node_key")
        _require_identifier(self.node_type, "NodeSpec.node_type")
        _require_identifier(self.node_name, "NodeSpec.node_name")
        if self.parent_node is not None:
            _require_qualified_node(self.parent_node, "NodeSpec.parent_node")
        parameters = _sequence(
            self.parameters, "NodeSpec.parameters", _MAX_PARMS_PER_NODE
        )
        parm_names: set[str] = set()
        for item in parameters:
            if type(item) is not ParmAssignment:
                raise TypeError(
                    "NodeSpec.parameters must contain ParmAssignment"
                )
            if item.parm_name in parm_names:
                raise ValueError("NodeSpec.parameters contains duplicate parameter")
            parm_names.add(item.parm_name)
        inputs = _sequence(self.inputs, "NodeSpec.inputs", _MAX_INPUTS_PER_NODE)
        input_indexes: set[int] = set()
        for item in inputs:
            if type(item) is not InputBinding:
                raise TypeError("NodeSpec.inputs must contain InputBinding")
            if item.input_index in input_indexes:
                raise ValueError("NodeSpec.inputs contains duplicate input index")
            input_indexes.add(item.input_index)
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "inputs", inputs)

    def to_dict(self) -> dict[str, object]:
        return {
            "node_key": self.node_key,
            "node_type": self.node_type,
            "node_name": self.node_name,
            "parent_node": self.parent_node,
            "parameters": [item.to_dict() for item in self.parameters],
            "inputs": [item.to_dict() for item in self.inputs],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> NodeSpec:
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "node_key",
                    "node_type",
                    "node_name",
                    "parent_node",
                    "parameters",
                    "inputs",
                }
            ),
            "NodeSpec",
        )
        parameters = _sequence(
            data["parameters"], "NodeSpec.parameters", _MAX_PARMS_PER_NODE
        )
        inputs = _sequence(data["inputs"], "NodeSpec.inputs", _MAX_INPUTS_PER_NODE)
        return cls(
            node_key=data["node_key"],  # type: ignore[arg-type]
            node_type=data["node_type"],  # type: ignore[arg-type]
            node_name=data["node_name"],  # type: ignore[arg-type]
            parent_node=data["parent_node"],  # type: ignore[arg-type]
            parameters=tuple(ParmAssignment.from_dict(item) for item in parameters),
            inputs=tuple(InputBinding.from_dict(item) for item in inputs),
        )


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    component_id: str
    role: str
    depends_on: tuple[str, ...]
    nodes: tuple[NodeSpec, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.component_id, "ComponentSpec.component_id")
        _require_identifier(self.role, "ComponentSpec.role")
        dependencies = _identifier_sequence(
            self.depends_on, "ComponentSpec.depends_on", _MAX_COMPONENTS
        )
        if self.component_id in dependencies:
            raise ValueError("ComponentSpec cannot depend on itself")
        nodes = _sequence(
            self.nodes, "ComponentSpec.nodes", _MAX_NODES_PER_COMPONENT
        )
        if not nodes:
            raise ValueError("ComponentSpec.nodes must contain at least one node")
        node_keys: set[str] = set()
        node_names: set[str] = set()
        for item in nodes:
            if type(item) is not NodeSpec:
                raise TypeError("ComponentSpec.nodes must contain NodeSpec")
            if item.node_key in node_keys:
                raise ValueError("ComponentSpec.nodes contains duplicate node keys")
            if item.node_name in node_names:
                raise ValueError("ComponentSpec.nodes contains duplicate node names")
            node_keys.add(item.node_key)
            node_names.add(item.node_name)
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "nodes", nodes)

    def to_dict(self) -> dict[str, object]:
        return {
            "component_id": self.component_id,
            "role": self.role,
            "depends_on": list(self.depends_on),
            "nodes": [item.to_dict() for item in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ComponentSpec:
        data = _require_exact_keys(
            value,
            frozenset({"component_id", "role", "depends_on", "nodes"}),
            "ComponentSpec",
        )
        nodes = _sequence(
            data["nodes"], "ComponentSpec.nodes", _MAX_NODES_PER_COMPONENT
        )
        return cls(
            component_id=data["component_id"],  # type: ignore[arg-type]
            role=data["role"],  # type: ignore[arg-type]
            depends_on=data["depends_on"],  # type: ignore[arg-type]
            nodes=tuple(NodeSpec.from_dict(item) for item in nodes),
        )


def _component_dependency_closure(
    components: tuple[ComponentSpec, ...],
) -> dict[str, frozenset[str]]:
    by_id = {component.component_id: component for component in components}
    for component in components:
        for dependency in component.depends_on:
            if dependency not in by_id:
                raise ValueError(
                    f"ProceduralSpec component references unknown component {dependency!r}"
                )

    visiting: set[str] = set()
    complete: dict[str, frozenset[str]] = {}

    def visit(component_id: str) -> frozenset[str]:
        if component_id in complete:
            return complete[component_id]
        if component_id in visiting:
            raise ValueError("ProceduralSpec component dependency cycle")
        visiting.add(component_id)
        result: set[str] = set()
        for dependency in by_id[component_id].depends_on:
            result.add(dependency)
            result.update(visit(dependency))
        visiting.remove(component_id)
        complete[component_id] = frozenset(result)
        return complete[component_id]

    for component_id in by_id:
        visit(component_id)
    return complete


@dataclass(frozen=True, slots=True)
class ProceduralSpec:
    spec_key: str
    brief_digest: str
    quality_profile_id: str
    workspace_root_node_id: str
    components: tuple[ComponentSpec, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ProceduralSpec.schema_version must be 1")
        _require_identifier(self.spec_key, "ProceduralSpec.spec_key")
        _require_sha256(self.brief_digest, "ProceduralSpec.brief_digest")
        _require_identifier(
            self.quality_profile_id, "ProceduralSpec.quality_profile_id"
        )
        _require_identifier(
            self.workspace_root_node_id, "ProceduralSpec.workspace_root_node_id"
        )
        components = _sequence(
            self.components, "ProceduralSpec.components", _MAX_COMPONENTS
        )
        if not components:
            raise ValueError("ProceduralSpec.components must not be empty")
        by_id: dict[str, ComponentSpec] = {}
        qualified_nodes: set[str] = set()
        node_names: set[str] = set()
        for component in components:
            if type(component) is not ComponentSpec:
                raise TypeError(
                    "ProceduralSpec.components must contain ComponentSpec"
                )
            if component.component_id in by_id:
                raise ValueError("ProceduralSpec.components contains duplicate IDs")
            by_id[component.component_id] = component
            for node in component.nodes:
                qualified = f"{component.component_id}.{node.node_key}"
                qualified_nodes.add(qualified)
                if node.node_name in node_names:
                    raise ValueError(
                        "ProceduralSpec contains duplicate node names under the Workspace root"
                    )
                node_names.add(node.node_name)
        closure = _component_dependency_closure(components)  # also cycle check
        for component in components:
            allowed_dependencies = closure[component.component_id]
            for node in component.nodes:
                references = [
                    *([node.parent_node] if node.parent_node is not None else []),
                    *(binding.source_node for binding in node.inputs),
                ]
                for reference in references:
                    assert reference is not None
                    if reference not in qualified_nodes:
                        raise ValueError(
                            f"ProceduralSpec references unknown qualified node {reference!r}"
                        )
                    source_component = reference.split(".", 1)[0]
                    if (
                        source_component != component.component_id
                        and source_component not in allowed_dependencies
                    ):
                        raise ValueError(
                            "ProceduralSpec contains a hidden dependency not declared "
                            "by ComponentSpec.depends_on"
                        )
        object.__setattr__(self, "components", components)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_dumps(self.to_dict()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "spec_key": self.spec_key,
            "brief_digest": self.brief_digest,
            "quality_profile_id": self.quality_profile_id,
            "workspace_root_node_id": self.workspace_root_node_id,
            "components": [item.to_dict() for item in self.components],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProceduralSpec:
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "spec_key",
                    "brief_digest",
                    "quality_profile_id",
                    "workspace_root_node_id",
                    "components",
                }
            ),
            "ProceduralSpec",
        )
        components = _sequence(
            data["components"], "ProceduralSpec.components", _MAX_COMPONENTS
        )
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            spec_key=data["spec_key"],  # type: ignore[arg-type]
            brief_digest=data["brief_digest"],  # type: ignore[arg-type]
            quality_profile_id=data["quality_profile_id"],  # type: ignore[arg-type]
            workspace_root_node_id=data["workspace_root_node_id"],  # type: ignore[arg-type]
            components=tuple(ComponentSpec.from_dict(item) for item in components),
        )


@dataclass(frozen=True, slots=True)
class QualityProfile:
    profile_id: str
    validators: tuple[ValidatorKind, ...]
    max_compiled_nodes: int
    max_parameter_samples: int
    max_repairs_per_stage: int
    allow_vex_source: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("QualityProfile.schema_version must be 1")
        _require_identifier(self.profile_id, "QualityProfile.profile_id")
        validators = _sequence(
            self.validators, "QualityProfile.validators", len(ValidatorKind)
        )
        if validators != tuple(ValidatorKind):
            raise ValueError(
                "QualityProfile.validators must use the exact deterministic validator order"
            )
        _require_exact_int(
            self.max_compiled_nodes, "QualityProfile.max_compiled_nodes"
        )
        if not 1 <= self.max_compiled_nodes <= 256:
            raise ValueError("QualityProfile.max_compiled_nodes must be in 1..256")
        _require_exact_int(
            self.max_parameter_samples, "QualityProfile.max_parameter_samples"
        )
        if not 1 <= self.max_parameter_samples <= 64:
            raise ValueError(
                "QualityProfile.max_parameter_samples must be in 1..64"
            )
        _require_exact_int(
            self.max_repairs_per_stage, "QualityProfile.max_repairs_per_stage"
        )
        if not 0 <= self.max_repairs_per_stage <= 2:
            raise ValueError(
                "QualityProfile.max_repairs_per_stage must allow at most 2 repairs"
            )
        _require_exact_bool(self.allow_vex_source, "QualityProfile.allow_vex_source")
        if self.allow_vex_source:
            raise ValueError("Task 18-A QualityProfile must not allow VEX source")
        object.__setattr__(self, "validators", validators)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "validators": [item.value for item in self.validators],
            "max_compiled_nodes": self.max_compiled_nodes,
            "max_parameter_samples": self.max_parameter_samples,
            "max_repairs_per_stage": self.max_repairs_per_stage,
            "allow_vex_source": self.allow_vex_source,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> QualityProfile:
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "profile_id",
                    "validators",
                    "max_compiled_nodes",
                    "max_parameter_samples",
                    "max_repairs_per_stage",
                    "allow_vex_source",
                }
            ),
            "QualityProfile",
        )
        validators = _sequence(
            data["validators"], "QualityProfile.validators", len(ValidatorKind)
        )
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            profile_id=data["profile_id"],  # type: ignore[arg-type]
            validators=tuple(ValidatorKind(item) for item in validators),
            max_compiled_nodes=data["max_compiled_nodes"],  # type: ignore[arg-type]
            max_parameter_samples=data["max_parameter_samples"],  # type: ignore[arg-type]
            max_repairs_per_stage=data["max_repairs_per_stage"],  # type: ignore[arg-type]
            allow_vex_source=data["allow_vex_source"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    validator: ValidatorKind
    count: int

    def __post_init__(self) -> None:
        if type(self.validator) is not ValidatorKind:
            raise ValueError("RepairAttempt.validator must be a ValidatorKind")
        _require_exact_int(self.count, "RepairAttempt.count")
        if not 1 <= self.count <= 2:
            raise ValueError("RepairAttempt.count must be in 1..2")

    def to_dict(self) -> dict[str, object]:
        return {"validator": self.validator.value, "count": self.count}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RepairAttempt:
        data = _require_exact_keys(
            value, frozenset({"validator", "count"}), "RepairAttempt"
        )
        return cls(
            validator=ValidatorKind(data["validator"]),
            count=data["count"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class RepairBudget:
    max_attempts_per_stage: int
    attempts: tuple[RepairAttempt, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("RepairBudget.schema_version must be 1")
        _require_exact_int(
            self.max_attempts_per_stage, "RepairBudget.max_attempts_per_stage"
        )
        if not 0 <= self.max_attempts_per_stage <= 2:
            raise ValueError(
                "RepairBudget.max_attempts_per_stage must allow at most 2 attempts"
            )
        items = _sequence(
            self.attempts, "RepairBudget.attempts", len(ValidatorKind)
        )
        seen: set[ValidatorKind] = set()
        for item in items:
            if type(item) is not RepairAttempt:
                raise TypeError("RepairBudget.attempts must contain RepairAttempt")
            if item.validator in seen:
                raise ValueError("RepairBudget.attempts contains duplicate stages")
            if item.count > self.max_attempts_per_stage:
                raise ValueError("RepairBudget attempt exceeds the configured maximum")
            seen.add(item.validator)
        object.__setattr__(
            self, "attempts", tuple(sorted(items, key=lambda item: item.validator.value))
        )

    def used(self, validator: ValidatorKind) -> int:
        for item in self.attempts:
            if item.validator is validator:
                return item.count
        return 0

    def remaining(self, validator: ValidatorKind) -> int:
        return self.max_attempts_per_stage - self.used(validator)

    def record(self, validator: ValidatorKind) -> RepairBudget:
        if type(validator) is not ValidatorKind:
            raise TypeError("validator must be an exact ValidatorKind")
        current = self.used(validator)
        if current >= self.max_attempts_per_stage:
            raise ValueError(f"RepairBudget stage {validator.value} is exhausted")
        updated = [
            item for item in self.attempts if item.validator is not validator
        ]
        updated.append(RepairAttempt(validator, current + 1))
        return RepairBudget(
            max_attempts_per_stage=self.max_attempts_per_stage,
            attempts=tuple(updated),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "max_attempts_per_stage": self.max_attempts_per_stage,
            "attempts": [item.to_dict() for item in self.attempts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RepairBudget:
        data = _require_exact_keys(
            value,
            frozenset(
                {"schema_version", "max_attempts_per_stage", "attempts"}
            ),
            "RepairBudget",
        )
        attempts = _sequence(
            data["attempts"], "RepairBudget.attempts", len(ValidatorKind)
        )
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            max_attempts_per_stage=data["max_attempts_per_stage"],  # type: ignore[arg-type]
            attempts=tuple(RepairAttempt.from_dict(item) for item in attempts),
        )


@dataclass(frozen=True, slots=True)
class RepairTicket:
    ticket_id: str
    validator: ValidatorKind
    failure_code: str
    message: str
    evidence_digests: tuple[str, ...]
    failed_parameter_samples: tuple[str, ...]
    replay_boundary_digest: str
    attempt: int
    status: RepairStatus
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("RepairTicket.schema_version must be 1")
        _require_identifier(self.ticket_id, "RepairTicket.ticket_id")
        if type(self.validator) is not ValidatorKind:
            raise ValueError("RepairTicket.validator must be a ValidatorKind")
        if (
            type(self.failure_code) is not str
            or _CODE_RE.fullmatch(self.failure_code) is None
        ):
            raise ValueError("RepairTicket.failure_code must be a namespaced code")
        _require_text(self.message, "RepairTicket.message", _MAX_TEXT)
        evidence = _sequence(
            self.evidence_digests,
            "RepairTicket.evidence_digests",
            _MAX_EVIDENCE,
        )
        normalized_evidence: list[str] = []
        seen_evidence: set[str] = set()
        for item in evidence:
            digest = _require_sha256(item, "RepairTicket.evidence_digests")
            if digest in seen_evidence:
                raise ValueError(
                    "RepairTicket.evidence_digests contains duplicates"
                )
            seen_evidence.add(digest)
            normalized_evidence.append(digest)
        samples = _sequence(
            self.failed_parameter_samples,
            "RepairTicket.failed_parameter_samples",
            _MAX_FAILED_SAMPLES,
        )
        normalized_samples: list[str] = []
        seen_samples: set[str] = set()
        for item in samples:
            sample = _require_text(
                item, "RepairTicket.failed_parameter_samples", _MAX_TITLE
            )
            if sample in seen_samples:
                raise ValueError(
                    "RepairTicket.failed_parameter_samples contains duplicates"
                )
            seen_samples.add(sample)
            normalized_samples.append(sample)
        _require_sha256(
            self.replay_boundary_digest, "RepairTicket.replay_boundary_digest"
        )
        _require_exact_int(self.attempt, "RepairTicket.attempt")
        if not 1 <= self.attempt <= 2:
            raise ValueError("RepairTicket.attempt must be in 1..2")
        if type(self.status) is not RepairStatus:
            raise ValueError("RepairTicket.status must be a RepairStatus")
        object.__setattr__(self, "evidence_digests", tuple(normalized_evidence))
        object.__setattr__(
            self, "failed_parameter_samples", tuple(normalized_samples)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "ticket_id": self.ticket_id,
            "validator": self.validator.value,
            "failure_code": self.failure_code,
            "message": self.message,
            "evidence_digests": list(self.evidence_digests),
            "failed_parameter_samples": list(self.failed_parameter_samples),
            "replay_boundary_digest": self.replay_boundary_digest,
            "attempt": self.attempt,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RepairTicket:
        data = _require_exact_keys(
            value,
            frozenset(
                {
                    "schema_version",
                    "ticket_id",
                    "validator",
                    "failure_code",
                    "message",
                    "evidence_digests",
                    "failed_parameter_samples",
                    "replay_boundary_digest",
                    "attempt",
                    "status",
                }
            ),
            "RepairTicket",
        )
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            ticket_id=data["ticket_id"],  # type: ignore[arg-type]
            validator=ValidatorKind(data["validator"]),
            failure_code=data["failure_code"],  # type: ignore[arg-type]
            message=data["message"],  # type: ignore[arg-type]
            evidence_digests=data["evidence_digests"],  # type: ignore[arg-type]
            failed_parameter_samples=data["failed_parameter_samples"],  # type: ignore[arg-type]
            replay_boundary_digest=data["replay_boundary_digest"],  # type: ignore[arg-type]
            attempt=data["attempt"],  # type: ignore[arg-type]
            status=RepairStatus(data["status"]),
        )


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate object key")
        result[key] = value
    return result


def _load_strict_json(raw: str | bytes, label: str) -> object:
    if type(raw) is str:
        data = raw.encode("utf-8")
    elif type(raw) is bytes:
        data = raw
    else:
        raise TypeError(f"{label} must be str or bytes")
    if len(data) > MAX_MODELING_JSON_BYTES:
        raise ValueError(f"{label} exceeds the maximum size")
    try:
        text = data.decode("utf-8")
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc


def parse_modeling_brief(raw: str | bytes) -> ModelingBrief:
    return ModelingBrief.from_dict(_load_strict_json(raw, "ModelingBrief JSON"))


def parse_procedural_spec(raw: str | bytes) -> ProceduralSpec:
    return ProceduralSpec.from_dict(_load_strict_json(raw, "ProceduralSpec JSON"))


def parse_quality_profile(raw: str | bytes) -> QualityProfile:
    return QualityProfile.from_dict(_load_strict_json(raw, "QualityProfile JSON"))


def parse_repair_budget(raw: str | bytes) -> RepairBudget:
    return RepairBudget.from_dict(_load_strict_json(raw, "RepairBudget JSON"))


def parse_repair_ticket(raw: str | bytes) -> RepairTicket:
    return RepairTicket.from_dict(_load_strict_json(raw, "RepairTicket JSON"))


__all__ = [
    "Axis",
    "BriefConstraint",
    "ComponentSpec",
    "FrontAxis",
    "InputBinding",
    "MAX_MODELING_JSON_BYTES",
    "ModelingBrief",
    "NodeSpec",
    "ParmAssignment",
    "ProceduralSpec",
    "QualityProfile",
    "RepairAttempt",
    "RepairBudget",
    "RepairStatus",
    "RepairTicket",
    "UnitSystem",
    "ValidatorKind",
    "parameter_value_shape",
    "parse_modeling_brief",
    "parse_procedural_spec",
    "parse_quality_profile",
    "parse_repair_budget",
    "parse_repair_ticket",
]
