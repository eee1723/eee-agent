"""Scene-level tools: status, reset, save .hip."""
from __future__ import annotations

from langchain_core.tools import tool

from eee_agent.bridge import hou_client
from eee_agent.config import resolve_path


@tool
def hou_status() -> dict:
    """Report Houdini connection status: version, current .hip file path, and FPS.

    Call this first to confirm the bridge is alive before doing any modeling.
    """
    def _fn(hou):
        return {
            "connected": True,
            "version": str(hou.applicationVersionString()),
            "hip": str(hou.hipFile.name()),
            "fps": float(hou.fps()),
        }

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"connected": False, "error": err}
    return res


@tool
def scene_reset(scope: str = "/obj") -> dict:
    """Delete every child node under ``scope`` (default /obj) for a clean build.

    Use this before reconstructing a model so stale nodes don't accumulate.
    Returns the count of deleted nodes.
    """
    def _fn(hou):
        parent = hou.node(scope)
        if parent is None:
            raise RuntimeError(f"scope not found: {scope}")
        children = list(parent.children())
        for c in children:
            c.destroy()
        return {"deleted": len(children), "scope": scope}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def save_hip(path: str) -> dict:
    """Save the current Houdini scene to ``path`` (.hip/.hiplc/.hipnc)."""
    def _fn(hou):
        full = resolve_path(path)
        hou.hipFile.save(full)
        return {"saved": True, "path": full}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}
