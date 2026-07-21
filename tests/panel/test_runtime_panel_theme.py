"""Token/QSS contract for the Pluto theme module (Qt-free)."""

from __future__ import annotations

import re

from houdini_side.runtime_panel import theme


def test_pluto_core_tokens_match_h22_defaults() -> None:
    assert theme.BG == "#2d2d2d"
    assert theme.SURFACE_LOWEST == "#242424"
    assert theme.SURFACE_HIGHEST == "#383838"
    assert theme.FG == "#dddddd"
    assert theme.FG_DIM == "#7f7f7f"
    assert theme.FIELD == "#434343"
    assert theme.BUTTON == "#474e62"
    assert theme.BUTTON_HOVER == "#777f95"
    assert theme.PRESSED == "#313f71"
    assert theme.PRIMARY == "#7082b9"
    assert theme.CHECKED_SURFACE == "#47578b"
    assert theme.HIGHLIGHT == "#fdba00"
    assert theme.HIGHLIGHT_FG == "#271900"
    assert theme.DIVIDER == "#202020"


def test_semantic_state_tokens() -> None:
    assert theme.STATUS_OK == "#73d114"
    assert theme.STATUS_WARN == "#f87431"
    assert theme.STATUS_ERROR == "#cc0000"


def test_qss_contains_tokens_and_no_unresolved_placeholders() -> None:
    qss = theme.build_qss()
    assert "#2d2d2d" in qss
    assert "#fdba00" in qss
    assert "QTabBar::tab" in qss
    assert "QPushButton" in qss
    assert not re.search(r"@[A-Za-z]+@", qss), "unresolved @Token@ placeholder"


def test_all_tokens_are_hex_colors() -> None:
    for name in dir(theme):
        if name.startswith("_"):
            continue
        if name.isupper() and name not in {
            "UI_FONT",
            "MONO_FONT",
            "UI_FONT_FALLBACK",
            "MONO_FONT_FALLBACK",
        }:
            value = getattr(theme, name)
            if isinstance(value, str):
                assert re.fullmatch(r"#[0-9a-f]{6}", value), name
