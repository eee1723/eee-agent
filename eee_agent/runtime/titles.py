"""Generate a short Session title from the first run (Qt-free, awaitable).

A Session is created with a placeholder title before the first run exists
(see the panel auto-create path). Once that run completes, the backend calls
:func:`generate_session_title` to rename it to something meaningful derived
from the user's prompt and the assistant reply. The call is best-effort:
any failure, timeout, or malformed output returns ``None`` and the
placeholder title is left intact.
"""

from __future__ import annotations

import asyncio
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

_LOG = logging.getLogger(__name__)

# Bounds: the model is asked to summarize, not to reason over the full reply,
# so the inputs are truncated well before the model's context window matters.
_MAX_PROMPT_CHARS = 500
_MAX_REPLY_CHARS = 500
_MAX_TITLE_CHARS = 40
_TIMEOUT_SECONDS = 8.0

_SYSTEM = (
    "You generate a concise Session title for a Houdini modeling task. "
    "Reply with ONLY the title: at most 40 characters, no quotes, no "
    "trailing punctuation, no prefix like 'Title:'. Prefer the user's intent "
    "in their own language (Chinese stays Chinese)."
)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit]


async def generate_session_title(
    model: BaseChatModel,
    user_input: str,
    *,
    final_response: str = "",
) -> str | None:
    """Return a short title for a Session, or ``None`` on any failure.

    The title is derived from the user prompt (primary) and the assistant
    reply (secondary). Both inputs are truncated before the model is called.
    """
    prompt = _truncate(user_input or "", _MAX_PROMPT_CHARS)
    if not prompt:
        # Nothing to summarize — leave the placeholder alone.
        return None
    reply = _truncate(final_response or "", _MAX_REPLY_CHARS)
    user_body = f"User request:\n{prompt}"
    if reply:
        user_body += f"\n\nAssistant reply:\n{reply}"
    try:
        message = await asyncio.wait_for(
            model.ainvoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=user_body),
            ]),
            timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        _LOG.warning("session title generation timed out")
        return None
    except Exception:  # noqa: BLE001 — best-effort, never block the run
        _LOG.warning("session title generation failed", exc_info=True)
        return None
    raw = message.content if hasattr(message, "content") else ""
    title = _normalize_title(raw)
    if not title:
        return None
    return title


def _normalize_title(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    # Strip the common "Title:" / quote wrapping models add despite instructions.
    text = raw.strip().strip('"').strip("'").strip()
    if text.lower().startswith("title:"):
        text = text[len("title:"):].strip()
    # Collapse internal newlines/whitespace into single spaces.
    text = " ".join(text.split())
    return text[:_MAX_TITLE_CHARS]
