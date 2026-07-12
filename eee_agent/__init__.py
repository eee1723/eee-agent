"""eee_agent — deepagents-based procedural modeling agent for Houdini 21.

Architecture (three processes, deps isolated):
  Houdini 21 process
    └─ hrpyc/RPC server (localhost:18811)  +  PySide6 chat panel (thin client)
  Agent process (this package, own venv)
    └─ deepagents + tools → rpyc → Houdini

See docs/architecture.md and the plan for details.
"""

__version__ = "0.1.0"
