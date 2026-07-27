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

    Only literal numeric parameters are included (menu parms are encoded as
    their integer index, e.g. torus ``orient`` and sweep2 ``surfaceshape``).
    Source/expression/file parameters and node types that were not verified
    are intentionally absent.

    torus / sphere / copyxform entries and the sweep2 tube-mode parms were
    probe-verified against live Houdini 21.0.440 on 2026-07-24 (see
    tests/modeling/catalog_probe_houdini.py).
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
                node_type="boolean2",
                create_type="boolean::2.0",
                parameters=(
                    ParmDefinition("booleanop", 0),
                    ParmDefinition("subtractchoices", 0),
                ),
                max_inputs=2,
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
                node_type="line",
                parameters=(
                    ParmDefinition("originx", 0.0),
                    ParmDefinition("originy", 0.0),
                    ParmDefinition("originz", 0.0),
                    ParmDefinition("dirx", 0.0),
                    ParmDefinition("diry", 1.0),
                    ParmDefinition("dirz", 0.0),
                    ParmDefinition("dist", 1.0),
                    ParmDefinition("points", 2),
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
                node_type="copytopoints2",
                create_type="copytopoints::2.0",
                parameters=(
                    ParmDefinition("pack", 0),
                    ParmDefinition("pivot", 1),
                    ParmDefinition("transform", 1),
                ),
                max_inputs=2,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="copyxform",
                parameters=(
                    ParmDefinition("ncy", 2),
                    ParmDefinition("rx", 0.0),
                    ParmDefinition("ry", 0.0),
                    ParmDefinition("rz", 0.0),
                    ParmDefinition("px", 0.0),
                    ParmDefinition("py", 0.0),
                    ParmDefinition("pz", 0.0),
                ),
                max_inputs=1,
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
                node_type="resample",
                parameters=(
                    ParmDefinition("length", 0.1),
                    ParmDefinition("dosegs", 0),
                    ParmDefinition("segs", 10),
                    ParmDefinition("treatpolysas", 0),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="sphere",
                parameters=(
                    ParmDefinition("radx", 1.0),
                    ParmDefinition("rady", 1.0),
                    ParmDefinition("radz", 1.0),
                    ParmDefinition("tx", 0.0),
                    ParmDefinition("ty", 0.0),
                    ParmDefinition("tz", 0.0),
                    ParmDefinition("rows", 13),
                    ParmDefinition("cols", 24),
                ),
                max_inputs=1,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="torus",
                parameters=(
                    ParmDefinition("radx", 1.0),
                    ParmDefinition("rady", 0.5),
                    ParmDefinition("tx", 0.0),
                    ParmDefinition("ty", 0.0),
                    ParmDefinition("tz", 0.0),
                    ParmDefinition("orient", 1),
                    ParmDefinition("rows", 12),
                    ParmDefinition("cols", 24),
                ),
                max_inputs=0,
                max_output_index=0,
            ),
            NodeTypeDefinition(
                node_type="sweep2",
                create_type="sweep::2.0",
                parameters=(
                    ParmDefinition("surfacetype", 5),
                    ParmDefinition("scale", 1.0),
                    ParmDefinition("roll", 0.0),
                    ParmDefinition("surfaceshape", 0),
                    ParmDefinition("radius", 0.1),
                    ParmDefinition("cols", 8),
                    ParmDefinition("endcaptype", 0),
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
