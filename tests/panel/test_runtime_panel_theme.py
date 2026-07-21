"""Token/QSS contract for the graphite-industrial theme module (Qt-free)."""

from __future__ import annotations

import re

from houdini_side.runtime_panel import theme


def test_graphite_core_tokens_match_dialog_palette() -> None:
    # Surfaces and accents track the SessionTitleDialog palette in client.py.
    assert theme.BG == "#17191d"
    assert theme.SURFACE_1 == "#14161a"
    assert theme.SURFACE_3 == "#20242a"
    assert theme.FG == "#e7e9ec"
    assert theme.FG_DIM == "#9097a1"
    assert theme.FIELD == "#14161a"
    assert theme.BUTTON == "#23282f"
    assert theme.BUTTON_HOVER == "#26383c"
    assert theme.PRIMARY == "#63c7c9"
    assert theme.CHECKED_SURFACE == "#29353a"
    assert theme.HIGHLIGHT == "#ff7a1a"
    assert theme.HIGHLIGHT_FG == "#1a0d00"
    assert theme.DIVIDER == "#2a2f37"


def test_semantic_state_tokens() -> None:
    assert theme.STATUS_OK == "#7bd88f"
    assert theme.STATUS_WARN == "#ff7a1a"
    assert theme.STATUS_ERROR == "#e45b55"


def test_qss_contains_tokens_and_no_unresolved_placeholders() -> None:
    qss = theme.build_qss()
    assert "#17191d" in qss
    assert "#ff7a1a" in qss
    assert "#63c7c9" in qss
    assert "QTabBar::tab" in qss
    assert "QPushButton" in qss
    assert not re.search(r"\{[a-z_0-9]+\}", qss), "unresolved {token} placeholder"


def test_all_tokens_are_hex_colors() -> None:
    for name in dir(theme):
        if name.startswith("_"):
            continue
        if name.isupper() and name not in {
            "UI_FONT",
            "MONO_FONT",
            "TITLE_FONT",
            "UI_FONT_FALLBACK",
            "MONO_FONT_FALLBACK",
        }:
            value = getattr(theme, name)
            if isinstance(value, str):
                assert re.fullmatch(r"#[0-9a-f]{6}", value), name
