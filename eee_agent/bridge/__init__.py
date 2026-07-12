"""Houdini RPC bridge.

We reimplement hrpyc's tiny client inline (rpyc.classic.connect + module fetch)
instead of vendoring Houdini's hrpyc.py: that file pulls in the `future` package
and a time-sync wrapper that only matters for animation. For SOP-based procedural
modeling neither is needed, so we keep the agent venv dependency-free of `future`.

The server side (houdini_side/start_rpc.py) runs inside Houdini and uses Houdini's
own rpyc directly, binding to localhost only (hrpyc.start_server binds 0.0.0.0).
"""
from eee_agent.bridge.hou_client import connect, disconnect, get_hou, is_connected, run

__all__ = ["connect", "disconnect", "get_hou", "is_connected", "run"]
