"""Houdini tool set exposed to the deepagents agent.

Design: low-level, orthogonal, composable. No "build a building" mega-tool — the
procedural recipe lives in skills/parametric-building/SKILL.md. Python (HOM) is
only glue here (create nodes, set parms, read back); heavy lifting is native SOP
nodes + VEX injected via set_vex.
"""
