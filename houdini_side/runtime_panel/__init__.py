"""Three-pane Runtime panel package. Public pypanel contract lives here.

``main_window`` (and its PySide6 dependency) is imported lazily so Qt-free
submodules (``theme``, ``view_models``, ``backend_launcher``) stay
importable in the plain test venv without PySide6; the widget is only
imported when the panel contract is actually used (inside Houdini).
"""

from __future__ import annotations

from houdini_side.runtime_panel import theme

__all__ = ["RuntimePanel", "create_panel", "open_panel"]


def create_panel():
    theme.register_fonts()
    from houdini_side.runtime_panel.main_window import RuntimePanel

    return RuntimePanel()


def open_panel():  # kept for houdini_side.launch compatibility
    panel = create_panel()
    panel.show()
    return panel


def __getattr__(name: str):
    if name == "RuntimePanel":
        from houdini_side.runtime_panel.main_window import RuntimePanel

        return RuntimePanel
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
