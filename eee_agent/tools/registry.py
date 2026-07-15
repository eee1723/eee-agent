"""Aggregate the Houdini tool set for create_deep_agent."""
from __future__ import annotations

from eee_agent.tools import compose, inspect, knowledge, nodes, procedural, scene, vex

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
    # knowledge graph (read-only, offline documentation cache)
    knowledge.search_houdini_knowledge,
    knowledge.get_houdini_knowledge,
]


def all_tools():
    return list(ALL_TOOLS)
