"""Entity ID construction and source-path normalization.

These helpers are pure and import-safe: they touch no filesystem, database
or HFS. ``normalize_source_path`` turns a machine-specific zip entry path
(which may use backslashes) into a safe logical POSIX path and rejects
absolute, drive-prefixed, traversal and empty inputs.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from eee_agent.knowledge.models import EntityKind

__all__ = ["make_entity_id", "normalize_source_path"]

# A Windows drive prefix (e.g. ``C:``). After backslash replacement a drive
# like ``C:\\houdini`` becomes ``C:/houdini``; POSIX path parsing would treat
# ``C:`` as an ordinary filename component, so it must be rejected explicitly.
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _contains_ascii_control(text: str) -> bool:
    return any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in text)


def normalize_source_path(raw: str) -> str:
    """Normalize a source path to a safe logical POSIX path.

    Backslashes are converted to forward slashes. The result never contains a
    drive prefix, a leading separator, an empty component or a ``.``/``..``
    segment. Empty or control-character inputs are rejected.
    """
    if not isinstance(raw, str):
        raise TypeError("source path must be a string")
    if not raw or not raw.strip():
        raise ValueError("unsafe source path: empty path")
    if _contains_ascii_control(raw):
        raise ValueError("unsafe source path: ASCII control character")

    text = raw.replace("\\", "/")
    if _DRIVE_PREFIX.match(text):
        raise ValueError("unsafe source path: drive prefix is not a logical path")
    if text.startswith("/"):
        raise ValueError("unsafe source path: absolute path")

    for segment in text.split("/"):
        if segment in ("", ".", ".."):
            raise ValueError("unsafe source path: traversal or empty segment")

    normalized = str(PurePosixPath(text))
    if normalized in ("", "."):
        raise ValueError("unsafe source path: empty path")
    return normalized


def make_entity_id(kind: EntityKind, key: str) -> str:
    """Build a stable, kind-prefixed entity id without case-folding.

    The key is a document-identity string (it may contain ``/``, ``@``,
    ``#`` and ``.``) and is preserved verbatim. Only empty or control-bearing
    keys are rejected; the kind's enum value supplies the prefix.
    """
    if not isinstance(key, str):
        raise TypeError("entity key must be a string")
    if not key:
        raise ValueError("unsafe entity key: empty key")
    if _contains_ascii_control(key):
        raise ValueError("unsafe entity key: ASCII control character")
    return f"{kind.value}:{key}"
