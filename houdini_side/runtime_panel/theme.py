"""Graphite-industrial design tokens and QSS builder for the Runtime panel.

Qt-free: tokens and ``build_qss`` are importable in the plain test venv.
Qt helpers (font registration, icon lookup) import PySide6 lazily inside
functions so this module never breaks the frozen test environment.

Visual language: the SessionTitleDialog palette (graphite surfaces, iron
borders, cyan primary accent, amber gate) promoted to the whole panel —
sharp 3px corners, outlined controls, mono kickers and status readouts.
No hex color literal may appear in any other runtime_panel module.
"""

from __future__ import annotations

# --- surfaces -------------------------------------------------------------
BG = "#17191d"                 # graphite: app background
SURFACE_LOWEST = "#101215"     # deepest well (scrollbars, sunken areas)
SURFACE_1 = "#14161a"          # inset: fields, lists, tab panes
SURFACE_2 = "#1b1f24"          # raised card surface
SURFACE_3 = "#20242a"          # slate: rails, headers, drawers
SURFACE_4 = "#23282f"          # hovered / active surface
SURFACE_HIGHEST = "#262b33"
TOOLTIP_SURFACE = "#0c0e10"
DIVIDER = "#2a2f37"            # subtle hairline between cards
PANE_DIVIDER = "#0e1013"       # splitter handle / pane seams

# --- text -----------------------------------------------------------------
FG = "#e7e9ec"
FG_PROMINENT = "#ffffff"
FG_DIM = "#9097a1"
FG_DIMMER = "#6a7079"
FIELD_FG = "#eef1f4"

# --- fields / views -------------------------------------------------------
FIELD = "#14161a"
FIELD_ALT = "#181b20"
VIEW_SURFACE = "#14161a"
VIEW_SURFACE_ALT = "#1b1f24"

# --- buttons / selection --------------------------------------------------
BUTTON = "#23282f"
BUTTON_HOVER = "#26383c"       # cyan-tinted hover wash
PRESSED = "#1d2a2e"
PRESSED_FG = "#e7e9ec"
PRIMARY = "#63c7c9"            # cyan: the one accent hue
CHECKED_SURFACE = "#29353a"
CHECKED_FG = "#e7e9ec"

# --- conversation accent (user bubbles + active emphasis) -----------------
# Cyan-leaning surfaces keep user input distinct from assistant replies
# without introducing a second hue family.
ACCENT = "#63c7c9"
ACCENT_FG = "#e7e9ec"
ACCENT_SURFACE = "#1f3436"
THINKING_SURFACE = "#121417"

# --- approval gate amber (reserved for pending authorization) -------------
HIGHLIGHT = "#ff7a1a"
HIGHLIGHT_FG = "#1a0d00"
HIGHLIGHT_SURFACE = "#3b2b20"

# --- semantic status ------------------------------------------------------
STATUS_OK = "#7bd88f"
STATUS_WARN = "#ff7a1a"
STATUS_ERROR = "#e45b55"

# --- fonts ----------------------------------------------------------------
UI_FONT = "SideFX Source Sans Pro"     # fallback: Segoe UI
MONO_FONT = "SideFX Source Code Pro"   # fallback: Consolas
TITLE_FONT = "Bahnschrift SemiCondensed"  # fallback: Segoe UI
UI_FONT_FALLBACK = "Segoe UI"
MONO_FONT_FALLBACK = "Consolas"

_QSS = """
QWidget {{ background: {bg}; color: {fg}; font-size: 9pt; }}
QLabel#DimLabel {{ color: {fg_dim}; }}
QLabel#ProminentLabel {{ color: {fg_prominent};
    font-family: "{title_font}", "{ui_font_fallback}";
    font-size: 11pt; font-weight: 600; }}
QLabel#Kicker {{ color: {fg_dim}; font-family: "{mono_font_fallback}";
    font-size: 8pt; font-weight: 700; }}
QLabel#StateLabel {{ color: {fg_dim}; font-family: "{mono_font_fallback}";
    font-size: 8pt; }}
QWidget#ContextBar {{ background: {surface3};
    border-bottom: 1px solid {divider}; }}
QWidget#SessionSidebar {{ background: {surface2};
    border-right: 1px solid {divider}; }}
QWidget#InspectorPane {{ background: {surface2};
    border-left: 1px solid {divider}; }}
QFrame#Card {{ background: {surface2}; border: 1px solid {divider};
    border-radius: 3px; }}
QFrame#GateCard {{ background: {surface3}; border: 1px solid {highlight};
    border-radius: 3px; }}
QFrame#UserBubble {{ background: {accent_surface}; color: {accent_fg};
    border: 1px solid {accent}; border-left: 3px solid {accent};
    border-radius: 3px; }}
QFrame#AssistantCard {{ background: {surface3}; border: 1px solid {divider};
    border-left: 3px solid {primary}; border-radius: 3px; }}
QFrame#ThinkingBlock {{ background: {thinking_surface};
    border: 1px solid {divider}; border-radius: 3px; }}
QToolButton#ThinkingToggle {{ background: transparent; border: none;
    color: {fg_dim}; font-family: "{mono_font_fallback}"; font-size: 8pt;
    text-align: left; }}
QToolButton#ThinkingToggle:checked {{ color: {primary}; }}
QPushButton {{ background: transparent; color: {primary};
    border: 1px solid {primary}; border-radius: 3px;
    padding: 6px 12px; font-weight: 600; }}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ color: {fg_dimmer}; border-color: {divider};
    background: transparent; }}
QPushButton#DangerButton {{ color: {status_error};
    border-color: {status_error}; }}
QPushButton#DangerButton:hover {{ background: #3b2526; }}
QPushButton#DangerButton:disabled {{ color: {fg_dimmer};
    border-color: {divider}; background: transparent; }}
QPushButton#GateApprove {{ color: {highlight};
    border-color: {highlight}; }}
QPushButton#GateApprove:hover {{ background: {highlight_surface}; }}
QPushButton#GateApprove:disabled {{ color: {fg_dimmer};
    border-color: {divider}; background: transparent; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {field}; color: {field_fg}; border: 1px solid {divider};
    border-radius: 3px; padding: 2px 6px; min-height: 17px;
    selection-background-color: {checked_surface};
    selection-color: {checked_fg}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {primary}; }}
QComboBox QAbstractItemView {{ background: {surface3}; color: {fg};
    border: 1px solid {divider};
    selection-background-color: {checked_surface}; }}
QListWidget, QTreeWidget, QTableWidget {{
    background: {view_surface}; color: {fg};
    border: 1px solid {divider}; border-radius: 3px;
    alternate-background-color: {view_surface_alt}; outline: 0; }}
QListWidget::item, QTreeWidget::item {{ padding: 4px 6px; border: none; }}
QListWidget::item:hover, QTreeWidget::item:hover {{
    background: {surface4}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {checked_surface}; color: {checked_fg};
    border-left: 2px solid {primary}; }}
QHeaderView::section {{ background: {surface3}; color: {fg_dim};
    border: none; border-bottom: 1px solid {divider}; padding: 4px 6px;
    font-family: "{mono_font_fallback}"; font-size: 8pt; font-weight: 700; }}
QTabWidget::pane {{ border: 1px solid {divider}; border-radius: 3px;
    background: {surface1}; top: -1px; }}
QTabBar::tab {{ background: {bg}; color: {fg_dim};
    border: 1px solid {divider}; border-bottom: none;
    padding: 5px 12px; font-family: "{mono_font_fallback}";
    font-size: 8pt; font-weight: 700; }}
QTabBar::tab:selected {{ background: {surface1}; color: {primary}; }}
QTabBar::tab:hover {{ color: {fg}; }}
QToolButton {{ background: transparent; color: {fg_dim}; border: none;
    border-radius: 3px; padding: 2px 8px;
    font-family: "{mono_font_fallback}"; font-size: 8pt; font-weight: 700; }}
QToolButton:hover {{ color: {fg}; background: {surface4}; }}
QToolButton:checked {{ color: {primary}; background: {pressed}; }}
QSplitter::handle {{ background: {pane_divider}; }}
QScrollBar:vertical {{ background: {surface_lowest}; width: 11px;
    border: none; }}
QScrollBar::handle:vertical {{ background: {button}; min-height: 30px;
    border-radius: 3px; margin: 1px; }}
QScrollBar::handle:vertical:hover {{ background: {button_hover}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ background: {surface_lowest}; height: 11px;
    border: none; }}
QScrollBar::handle:horizontal {{ background: {button}; min-width: 30px;
    border-radius: 3px; margin: 1px; }}
QScrollBar::handle:horizontal:hover {{ background: {button_hover}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0; }}
QToolTip {{ background: {tooltip_surface}; color: {fg};
    border: 1px solid {divider}; }}
"""


def build_qss() -> str:
    """Render the panel stylesheet from the tokens above."""
    return _QSS.format(
        bg=BG, fg=FG, fg_dim=FG_DIM, fg_dimmer=FG_DIMMER,
        fg_prominent=FG_PROMINENT, field=FIELD, field_fg=FIELD_FG,
        surface_lowest=SURFACE_LOWEST, surface1=SURFACE_1,
        surface2=SURFACE_2, surface3=SURFACE_3, surface4=SURFACE_4,
        divider=DIVIDER, pane_divider=PANE_DIVIDER,
        button=BUTTON, button_hover=BUTTON_HOVER, pressed=PRESSED,
        pressed_fg=PRESSED_FG, primary=PRIMARY,
        accent=ACCENT, accent_fg=ACCENT_FG, accent_surface=ACCENT_SURFACE,
        thinking_surface=THINKING_SURFACE,
        checked_surface=CHECKED_SURFACE, checked_fg=CHECKED_FG,
        highlight=HIGHLIGHT, highlight_fg=HIGHLIGHT_FG,
        highlight_surface=HIGHLIGHT_SURFACE,
        status_error=STATUS_ERROR,
        view_surface=VIEW_SURFACE, view_surface_alt=VIEW_SURFACE_ALT,
        tooltip_surface=TOOLTIP_SURFACE,
        title_font=TITLE_FONT, ui_font_fallback=UI_FONT_FALLBACK,
        mono_font_fallback=MONO_FONT_FALLBACK,
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


def mono_font_family() -> str:
    """Primary monospace font family name (no CSS-style fallback list).

    Use ``MONO_FONT_FALLBACK`` with ``QFont.insertSubstitution`` when the
    primary family is unavailable.
    """
    return MONO_FONT
