"""Derive a short Session title from the first run's prompt.

A Session is created with a placeholder title before the first run exists
(see the panel auto-create path). Once that run completes, the backend calls
:func:`derive_session_title` to rename it from the user's prompt. The title is
derived locally (no model, no network) so it is instantaneous and cannot time
out; any failure or empty input returns ``None`` and the placeholder title is
left intact.
"""

from __future__ import annotations

_MAX_TITLE_CHARS = 40


def derive_session_title(user_input: str) -> str | None:
    """Return a short title for a Session, or ``None`` if none can be derived.

    The title is the first non-empty line of the user's prompt, whitespace-
    collapsed and truncated to ``_MAX_TITLE_CHARS``. The prompt's own language
    (including Chinese) is preserved verbatim.
    """
    text = user_input or ""
    # Take the first meaningful line: the user's primary intent is usually
    # stated up front, and a long multi-line prompt makes a poor sidebar label.
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first_line:
        return None
    # Collapse internal runs of whitespace into single spaces.
    collapsed = " ".join(first_line.split())
    return collapsed[:_MAX_TITLE_CHARS] or None
