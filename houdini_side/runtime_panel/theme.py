"""Houdini 22 Pluto dark-theme design tokens and QSS builder.

Qt-free: tokens and ``build_qss`` are importable in the plain test venv.
Qt helpers (font registration, icon lookup) import PySide6 lazily inside
functions so this module never breaks the frozen test environment.

Token source: $HFS/houdini/config/Themes/PlutoThemeDefaults.json (default
Pluto dark palette) and $HFS/houdini/config/Styles/base.qss (metrics).
No hex color literal may appear in any other runtime_panel module.
"""

from __future__ import annotations

# --- surfaces -------------------------------------------------------------
BG = "#2d2d2d"
SURFACE_LOWEST = "#242424"
SURFACE_1 = "#282828"
SURFACE_2 = "#313131"
SURFACE_3 = "#333333"
SURFACE_4 = "#353535"
SURFACE_HIGHEST = "#383838"
TOOLTIP_SURFACE = "#171717"
DIVIDER = "#202020"
PANE_DIVIDER = "#222222"

# --- text -----------------------------------------------------------------
FG = "#dddddd"
FG_PROMINENT = "#ffffff"
FG_DIM = "#7f7f7f"
FG_DIMMER = "#5a5a5a"
FIELD_FG = "#f9f9f9"

# --- fields / views -------------------------------------------------------
FIELD = "#434343"
FIELD_ALT = "#3e3e3e"
VIEW_SURFACE = "#2f2f2f"
VIEW_SURFACE_ALT = "#353535"

# --- buttons / selection --------------------------------------------------
BUTTON = "#474e62"
BUTTON_HOVER = "#777f95"
PRESSED = "#313f71"
PRESSED_FG = "#f7f9ff"
PRIMARY = "#7082b9"
SECONDARY = "#7c849a"
CHECKED_SURFACE = "#47578b"
CHECKED_FG = "#e3ebff"

# --- approval gate amber (reserved for pending authorization) -------------
HIGHLIGHT = "#fdba00"
HIGHLIGHT_FG = "#271900"
HIGHLIGHT_SURFACE = "#755400"

# --- semantic status (H22 UIDark performance/status colors) ---------------
STATUS_OK = "#73d114"
STATUS_WARN = "#f87431"
STATUS_ERROR = "#cc0000"

# --- fonts ----------------------------------------------------------------
UI_FONT = "SideFX Source Sans Pro"     # fallback: Segoe UI
MONO_FONT = "SideFX Source Code Pro"   # fallback: Consolas
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT_FALLBACK = "Consolas"

_QSS = """
QWidget {{ background: {bg}; color: {fg}; font-size: 9pt; }}
QLabel#DimLabel {{ color: {fg_dim}; }}
QLabel#ProminentLabel {{ color: {fg_prominent}; font-weight: 600; }}
QFrame#Card {{ background: {surface2}; border: 1px solid {divider};
    border-radius: 5px; }}
QFrame#GateCard {{ background: {surface2}; border: 1px solid {highlight};
    border-radius: 5px; }}
QPushButton {{ background: {button}; color: {fg}; border: none;
    border-radius: 4px; padding: 2px 15px; min-height: 17px; }}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:pressed {{ background: {pressed}; color: {pressed_fg}; }}
QPushButton:disabled {{ background: {surface2}; color: {fg_dimmer}; }}
QPushButton#GateApprove {{ background: {highlight}; color: {highlight_fg}; }}
QPushButton#GateApprove:hover {{ background: {highlight_surface};
    color: {fg_prominent}; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {field}; color: {field_fg}; border: 1px solid {divider};
    border-radius: 2px; padding: 1px 4px; min-height: 17px;
    selection-background-color: {checked_surface};
    selection-color: {checked_fg}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {primary}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {view_surface}; color: {fg};
    border: 1px solid {divider};
    alternate-background-color: {view_surface_alt}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {checked_surface}; color: {checked_fg}; }}
QTabWidget::pane {{ border: 1px solid {divider}; }}
QTabBar::tab {{ background: {surface1}; color: {fg_dim};
    padding: 2px 7px; min-height: 20px; border: none; }}
QTabBar::tab:selected {{ background: {bg}; color: {fg_prominent}; }}
QTabBar::tab:hover {{ background: {surface2}; }}
QSplitter::handle {{ background: {pane_divider}; }}
QScrollBar:vertical {{ background: {surface_lowest}; width: 15px; }}
QScrollBar::handle:vertical {{ background: {button}; min-height: 30px;
    border-radius: 4px; }}
QScrollBar::handle:vertical:hover {{ background: {button_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: {tooltip_surface}; color: {fg};
    border: 1px solid {divider}; }}
"""


def build_qss() -> str:
    """Render the panel stylesheet from the tokens above."""
    return _QSS.format(
        bg=BG, fg=FG, fg_dim=FG_DIM, fg_dimmer=FG_DIMMER,
        fg_prominent=FG_PROMINENT, field=FIELD, field_fg=FIELD_FG,
        surface_lowest=SURFACE_LOWEST, surface1=SURFACE_1,
        surface2=SURFACE_2, divider=DIVIDER, pane_divider=PANE_DIVIDER,
        button=BUTTON, button_hover=BUTTON_HOVER, pressed=PRESSED,
        pressed_fg=PRESSED_FG, primary=PRIMARY,
        checked_surface=CHECKED_SURFACE, checked_fg=CHECKED_FG,
        highlight=HIGHLIGHT, highlight_fg=HIGHLIGHT_FG,
        highlight_surface=HIGHLIGHT_SURFACE, view_surface=VIEW_SURFACE,
        view_surface_alt=VIEW_SURFACE_ALT, tooltip_surface=TOOLTIP_SURFACE,
    )


def register_fonts() -> None:
    """Register SideFX fonts from $HFS/houdini/fonts when inside Houdini.

    No-op outside Houdini; PySide6 is imported lazily so the test venv
    (no Qt) can import this module.
    """
    try:
        import hou  # type: ignore  # noqa: F401  (availability probe)
        from PySide6 import QtGui
    except ImportError:
        return
    import os

    fonts_dir = os.path.join(os.environ.get("HFS", ""), "houdini", "fonts")
    if not os.path.isdir(fonts_dir):
        return
    for name in os.listdir(fonts_dir):
        if name.lower().endswith((".ttf", ".otf")):
            QtGui.QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))


def ui_font_family() -> str:
    """Primary UI font family name (Qt does not honor CSS fallback lists).

    ``register_fonts()`` makes the SideFX fonts available inside Houdini.
    Outside Houdini, or if registration fails, callers should pass the
    fallback via ``QFont.insertSubstitution(UI_FONT, [UI_FONT_FALLBACK])``
    or accept Qt's automatic matching.
    """
    return UI_FONT


def mono_font_family() -> str:
    """Primary monospace font family name (no CSS-style fallback list).

    See ``ui_font_family()`` — use ``MONO_FONT_FALLBACK`` with
    ``QFont.insertSubstitution`` when the primary family is unavailable.
    """
    return MONO_FONT


def icon(name: str):
    """Resolve a built-in Houdini icon, e.g. icon("BUTTONS_add.svg")."""
    import hou  # type: ignore

    return hou.qt.Icon(name, None)
