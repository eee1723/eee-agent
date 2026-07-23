"""Tests for the Session auto-title derivation (prompt-based, no LLM)."""

from __future__ import annotations

from eee_agent.runtime.titles import derive_session_title


def test_derives_title_from_user_prompt() -> None:
    # The user's own language is preserved verbatim.
    assert derive_session_title("帮我建一个带栏杆的楼梯") == "帮我建一个带栏杆的楼梯"
    assert derive_session_title("build a shelf") == "build a shelf"


def test_takes_first_line_of_multiline_prompt() -> None:
    prompt = "build a staircase with railings\n\nPlease make it parametric."
    assert derive_session_title(prompt) == "build a staircase with railings"


def test_truncates_overlong_title() -> None:
    title = derive_session_title("x" * 200)
    assert len(title) == 40
    assert title == "x" * 40


def test_collapses_internal_whitespace() -> None:
    assert derive_session_title("build   a\ttable") == "build a table"


def test_returns_none_on_empty_input() -> None:
    assert derive_session_title("") is None
    assert derive_session_title("   ") is None
    assert derive_session_title("\n\n  \n") is None


def test_strips_leading_and_trailing_whitespace() -> None:
    assert derive_session_title("  build a chair  ") == "build a chair"
