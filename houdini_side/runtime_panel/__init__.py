"""Three-pane Runtime panel package. Public pypanel contract lives here.

Re-exports are lazy (PEP 562) so Qt-free submodules (``theme``,
``view_models``, ``backend_launcher``) stay importable in the plain test
venv without PySide6; ``legacy`` is only imported when the panel contract
is actually accessed (inside Houdini).
"""

from __future__ import annotations

__all__ = ["RuntimePanel", "create_panel", "open_panel"]


def __getattr__(name: str):
    if name in __all__:
        from houdini_side.runtime_panel import legacy

        return getattr(legacy, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
