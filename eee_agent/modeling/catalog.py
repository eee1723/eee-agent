"""Houdini 21.0.440-verified minimal trusted modeling NodeCatalog."""

from __future__ import annotations

from eee_agent.modeling.compiler import (
    NodeCatalog,
    NodeTypeDefinition,
    ParmDefinition,
)
from eee_agent.modeling.contracts import QualityProfile, ValidatorKind


def houdini_21_minimal_catalog() -> NodeCatalog:
    """Return the small catalog verified with Houdini 21.0.440 hython.

    Only literal numeric parameters are included. Source/expression/file
    parameters and node types that were not verified are intentionally absent.
    """
    return NodeCatalog(
        entries=(
            NodeTypeDefinition(
                node_type="geo",
                parameters=(),
                max_inputs=1,
                max_output_index=0,
                can_parent_nodes=True,
            ),
            NodeTypeDefinition(
                node_type="box",
                parameters=(
                    ParmDefinition("sizex", 1.0),
                    ParmDefinition("sizey", 1.0),
                    ParmDefinition("sizez", 1.0),
                    ParmDefinition("tx", 0.0),
                    ParmDefinition("ty", 0.0),
                    ParmDefinition("tz", 0.0),
                    ParmDefinition("rx", 0.0),
                    ParmDefinition("ry", 0.0),
                    ParmDefinition("rz", 0.0),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="grid",
                parameters=(
                    ParmDefinition("sizex", 10.0),
                    ParmDefinition("sizey", 10.0),
                    ParmDefinition("rows", 10),
                    ParmDefinition("cols", 10),
                ),
                max_inputs=0,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="merge",
                parameters=(),
                max_inputs=64,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="null",
                parameters=(
                    ParmDefinition("cacheinput", 0),
                    ParmDefinition("copyinput", 1),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="normal",
                parameters=(
                    ParmDefinition("type", 1),
                    ParmDefinition("cuspangle", 60.0),
                    ParmDefinition("method", 1),
                    ParmDefinition("normalize", 0),
                    ParmDefinition("reverse", 0),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="fuse2",
                create_type="fuse::2.0",
                parameters=(
                    ParmDefinition("tol3d", 0.001),
                    ParmDefinition("consolidatesnappedpoints", 1),
                    ParmDefinition("deldegen", 1),
                ),
                max_inputs=2,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="polyextrude2",
                create_type="polyextrude::2.0",
                parameters=(
                    ParmDefinition("dist", 0.0),
                    ParmDefinition("inset", 0.0),
                    ParmDefinition("divs", 1),
                ),
                max_inputs=2,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="subdivide",
                parameters=(
                    ParmDefinition("algorithm", 2),
                    ParmDefinition("iterations", 1),
                    ParmDefinition("creaseweight", 0.0),
                    ParmDefinition("bias", 1.0),
                ),
                max_inputs=2,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="xform",
                parameters=(
                    ParmDefinition("tx", 0.0),
                    ParmDefinition("ty", 0.0),
                    ParmDefinition("tz", 0.0),
                    ParmDefinition("rx", 0.0),
                    ParmDefinition("ry", 0.0),
                    ParmDefinition("rz", 0.0),
                    ParmDefinition("sx", 1.0),
                    ParmDefinition("sy", 1.0),
                    ParmDefinition("sz", 1.0),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
        )
    )


def houdini_21_minimal_quality_profile() -> QualityProfile:
    """Return the deterministic profile paired with the verified catalog."""
    return QualityProfile(
        profile_id="houdini_21_minimal_v1",
        validators=tuple(ValidatorKind),
        max_compiled_nodes=64,
        max_parameter_samples=16,
        max_repairs_per_stage=2,
        allow_vex_source=False,
    )


__all__ = [
    "houdini_21_minimal_catalog",
    "houdini_21_minimal_quality_profile",
]
