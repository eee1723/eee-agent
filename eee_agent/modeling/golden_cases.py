"""Deterministic Golden Cases for the Houdini 21 minimal modeling catalog."""

from __future__ import annotations

from dataclasses import dataclass, replace

from eee_agent.modeling.contracts import (
    Axis,
    ComponentSpec,
    FrontAxis,
    InputBinding,
    ModelingBrief,
    NodeSpec,
    ParmAssignment,
    ProceduralSpec,
    UnitSystem,
)


@dataclass(frozen=True, slots=True)
class GoldenCase:
    case_id: str
    brief: ModelingBrief
    spec: ProceduralSpec
    expected_node_types: tuple[str, ...]
    expected_terminal_node_name: str

    def __post_init__(self) -> None:
        if type(self.case_id) is not str or not self.case_id:
            raise ValueError("GoldenCase.case_id must be non-empty")
        if type(self.brief) is not ModelingBrief:
            raise TypeError("GoldenCase.brief must be a ModelingBrief")
        if type(self.spec) is not ProceduralSpec:
            raise TypeError("GoldenCase.spec must be a ProceduralSpec")
        if self.spec.brief_digest != self.brief.digest:
            raise ValueError("GoldenCase spec must bind its brief")
        types = tuple(self.expected_node_types)
        actual = tuple(
            node.node_type
            for component in self.spec.components
            for node in component.nodes
        )
        if types != actual:
            raise ValueError("GoldenCase expected node types do not match its spec")
        if type(self.expected_terminal_node_name) is not str:
            raise TypeError("GoldenCase terminal name must be a string")
        names = {
            node.node_name
            for component in self.spec.components
            for node in component.nodes
        }
        if self.expected_terminal_node_name not in names:
            raise ValueError("GoldenCase terminal node is not present")
        object.__setattr__(self, "expected_node_types", types)

    def bind_workspace_root(self, node_id: str) -> ProceduralSpec:
        """Return the exact case bound to a trusted existing Workspace root."""
        return replace(self.spec, workspace_root_node_id=node_id)


def _brief(case_id: str, goal: str) -> ModelingBrief:
    return ModelingBrief(
        brief_key=case_id,
        title=case_id.replace("_", " ").title(),
        asset_family="golden_case",
        goal=goal,
        units=UnitSystem.CENTIMETERS,
        up_axis=Axis.Y,
        front_axis=FrontAxis.NEGATIVE_Z,
        constraints=(),
    )


def _case(
    case_id: str,
    goal: str,
    components: tuple[ComponentSpec, ...],
    terminal: str,
) -> GoldenCase:
    brief = _brief(case_id, goal)
    spec = ProceduralSpec(
        spec_key=f"{case_id}_v1",
        brief_digest=brief.digest,
        quality_profile_id="houdini_21_minimal_v1",
        workspace_root_node_id="bootstrap_root",
        components=components,
    )
    return GoldenCase(
        case_id=case_id,
        brief=brief,
        spec=spec,
        expected_node_types=tuple(
            node.node_type for component in components for node in component.nodes
        ),
        expected_terminal_node_name=terminal,
    )


def houdini_21_minimal_golden_cases() -> tuple[GoldenCase, ...]:
    """Return ordered cases covering source, transform, merge, and output."""
    box_chain = _case(
        "box_transform_output",
        "Build a sized box, translate it, and expose a stable output node.",
        (
            ComponentSpec(
                component_id="source",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="box",
                        node_type="box",
                        node_name="box1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("sizex", 2.0),
                            ParmAssignment("sizey", 3.0),
                            ParmAssignment("sizez", 4.0),
                        ),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="finish",
                role="output",
                depends_on=("source",),
                nodes=(
                    NodeSpec(
                        node_key="xform",
                        node_type="xform",
                        node_name="xform1",
                        parent_node=None,
                        parameters=(ParmAssignment("tx", 1.0),),
                        inputs=(InputBinding(0, "source.box", 0),),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "finish.xform", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    grid_chain = _case(
        "grid_transform_output",
        "Build a bounded grid, translate it, and expose a stable output node.",
        (
            ComponentSpec(
                component_id="source",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="grid",
                        node_type="grid",
                        node_name="grid1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("sizex", 4.0),
                            ParmAssignment("sizey", 6.0),
                            ParmAssignment("rows", 5),
                            ParmAssignment("cols", 7),
                        ),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="finish",
                role="output",
                depends_on=("source",),
                nodes=(
                    NodeSpec(
                        node_key="xform",
                        node_type="xform",
                        node_name="xform1",
                        parent_node=None,
                        parameters=(ParmAssignment("ty", 0.5),),
                        inputs=(InputBinding(0, "source.grid", 0),),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "finish.xform", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    merged = _case(
        "merged_sources_output",
        "Merge a box and grid into one stable output node.",
        (
            ComponentSpec(
                component_id="sources",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="box",
                        node_type="box",
                        node_name="box1",
                        parent_node=None,
                        parameters=(ParmAssignment("ty", 1.0),),
                        inputs=(),
                    ),
                    NodeSpec(
                        node_key="grid",
                        node_type="grid",
                        node_name="grid1",
                        parent_node=None,
                        parameters=(),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="finish",
                role="output",
                depends_on=("sources",),
                nodes=(
                    NodeSpec(
                        node_key="merge",
                        node_type="merge",
                        node_name="merge1",
                        parent_node=None,
                        parameters=(),
                        inputs=(
                            InputBinding(0, "sources.box", 0),
                            InputBinding(1, "sources.grid", 0),
                        ),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "finish.merge", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    surface = _case(
        "subdivided_surface_output",
        "Subdivide a box, compute normals, and expose a stable output node.",
        (
            ComponentSpec(
                component_id="source",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="box",
                        node_type="box",
                        node_name="box1",
                        parent_node=None,
                        parameters=(),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="surface",
                role="surface",
                depends_on=("source",),
                nodes=(
                    NodeSpec(
                        node_key="subdivide",
                        node_type="subdivide",
                        node_name="subdivide1",
                        parent_node=None,
                        parameters=(ParmAssignment("iterations", 2),),
                        inputs=(InputBinding(0, "source.box", 0),),
                    ),
                    NodeSpec(
                        node_key="normal",
                        node_type="normal",
                        node_name="normal1",
                        parent_node=None,
                        parameters=(ParmAssignment("normalize", 1),),
                        inputs=(InputBinding(0, "surface.subdivide", 0),),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "surface.normal", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    extruded = _case(
        "extruded_grid_output",
        "Extrude a grid, fuse it, compute normals, and expose a stable output.",
        (
            ComponentSpec(
                component_id="source",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="grid",
                        node_type="grid",
                        node_name="grid1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("sizex", 2.0),
                            ParmAssignment("sizey", 2.0),
                        ),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="surface",
                role="surface",
                depends_on=("source",),
                nodes=(
                    NodeSpec(
                        node_key="extrude",
                        node_type="polyextrude2",
                        node_name="polyextrude1",
                        parent_node=None,
                        parameters=(ParmAssignment("dist", 1.0),),
                        inputs=(InputBinding(0, "source.grid", 0),),
                    ),
                    NodeSpec(
                        node_key="fuse",
                        node_type="fuse2",
                        node_name="fuse1",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "surface.extrude", 0),),
                    ),
                    NodeSpec(
                        node_key="normal",
                        node_type="normal",
                        node_name="normal1",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "surface.fuse", 0),),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "surface.normal", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    copied = _case(
        "copied_box_output",
        "Copy a box onto a bounded line of points and expose one output.",
        (
            ComponentSpec(
                component_id="sources",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="box",
                        node_type="box",
                        node_name="box1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("sizex", 0.5),
                            ParmAssignment("sizey", 0.5),
                            ParmAssignment("sizez", 0.5),
                        ),
                        inputs=(),
                    ),
                    NodeSpec(
                        node_key="points",
                        node_type="line",
                        node_name="line1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("dirx", 1.0),
                            ParmAssignment("diry", 0.0),
                            ParmAssignment("dist", 4.0),
                            ParmAssignment("points", 5),
                        ),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="assembly",
                role="assembly",
                depends_on=("sources",),
                nodes=(
                    NodeSpec(
                        node_key="copy",
                        node_type="copytopoints2",
                        node_name="copytopoints1",
                        parent_node=None,
                        parameters=(),
                        inputs=(
                            InputBinding(0, "sources.box", 0),
                            InputBinding(1, "sources.points", 0),
                        ),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "assembly.copy", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    swept = _case(
        "swept_lines_output",
        "Sweep a line profile along a resampled path and expose one output.",
        (
            ComponentSpec(
                component_id="sources",
                role="generator",
                depends_on=(),
                nodes=(
                    NodeSpec(
                        node_key="profile",
                        node_type="line",
                        node_name="profile_line",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("dirx", 1.0),
                            ParmAssignment("diry", 0.0),
                            ParmAssignment("dist", 1.0),
                        ),
                        inputs=(),
                    ),
                    NodeSpec(
                        node_key="path",
                        node_type="line",
                        node_name="path_line",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("dist", 3.0),
                            ParmAssignment("points", 8),
                        ),
                        inputs=(),
                    ),
                ),
            ),
            ComponentSpec(
                component_id="assembly",
                role="assembly",
                depends_on=("sources",),
                nodes=(
                    NodeSpec(
                        node_key="resample",
                        node_type="resample",
                        node_name="resample1",
                        parent_node=None,
                        parameters=(
                            ParmAssignment("dosegs", 1),
                            ParmAssignment("segs", 8),
                        ),
                        inputs=(InputBinding(0, "sources.path", 0),),
                    ),
                    NodeSpec(
                        node_key="sweep",
                        node_type="sweep2",
                        node_name="sweep1",
                        parent_node=None,
                        parameters=(),
                        inputs=(
                            InputBinding(0, "assembly.resample", 0),
                            InputBinding(1, "sources.profile", 0),
                        ),
                    ),
                    NodeSpec(
                        node_key="out",
                        node_type="null",
                        node_name="OUT_MODEL",
                        parent_node=None,
                        parameters=(),
                        inputs=(InputBinding(0, "assembly.sweep", 0),),
                    ),
                ),
            ),
        ),
        "OUT_MODEL",
    )
    return box_chain, grid_chain, merged, surface, extruded, copied, swept


__all__ = ["GoldenCase", "houdini_21_minimal_golden_cases"]
