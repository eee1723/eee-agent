"""Aggregate the Houdini tool set for create_deep_agent."""
from __future__ import annotations

from langchain_core.tools import BaseTool

from eee_agent.tools import compose, inspect, nodes, procedural, scene, vex

ALL_TOOLS = [
    # scene
    scene.hou_status,
    scene.scene_reset,
    scene.save_hip,
    # nodes
    nodes.create_node,
    nodes.connect_nodes,
    nodes.set_parms,
    nodes.find_nodes,
    nodes.delete_node,
    nodes.describe_node_type,
    # vex
    vex.set_vex,
    # compose (multi-input helpers)
    compose.merge_nodes,
    compose.copy_to_points,
    # inspect / verify
    inspect.cook_node,
    inspect.geometry_stats,
    inspect.validate_geometry,
    inspect.export_geometry,
    # procedural / component-based (Phase C)
    procedural.ensure_work_container,
    procedural.add_root_parm,
    procedural.make_component,
    procedural.expose_anchors,
    procedural.wire_anchor,
    procedural.set_expression,
    procedural.assemble_output,
    procedural.work_status,
    procedural.anchor_graph,
]

# Explicit Runtime read-only allowlist. The Runtime agent may inspect the scene
# but never mutate it. This is an allowlist, not a name-filter over ALL_TOOLS,
# so the security boundary is auditable and cannot drift if a write tool is
# renamed or added.
READ_ONLY_TOOLS: list[BaseTool] = [
    scene.hou_status,
    nodes.find_nodes,
    nodes.describe_node_type,
    inspect.geometry_stats,
    inspect.validate_geometry,
    procedural.work_status,
    procedural.anchor_graph,
]


def all_tools() -> list[BaseTool]:
    return list(ALL_TOOLS)


def read_only_tools() -> list[BaseTool]:
    return list(READ_ONLY_TOOLS)
