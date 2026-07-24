"""Shared strict-JSON parsing helpers.

Single authority for the dup-key-rejecting ``object_pairs_hook`` and the
bounded strict loader used by every wire/persistence contract module
(``houdini_bridge``, ``runtime``, ``changesets``, ``modeling``, ``vision``,
``panel``). These helpers were previously copy-pasted across modules and had
already drifted in messages and structure; import them from here instead of
re-implementing.
"""

from __future__ import annotations

import json

__all__ = [
    "DuplicateKeyError",
    "load_strict_json",
    "reject_duplicate_keys",
]


class DuplicateKeyError(ValueError):
    """Raised by the JSON ``object_pairs_hook`` on any duplicate object key."""


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` that rejects duplicate keys at any object depth."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def load_strict_json(raw: object, label: str, *, max_bytes: int) -> object:
    """Parse ``str``/``bytes`` into a strict JSON value.

    Rejects non-str/bytes input, payloads over ``max_bytes`` UTF-8 bytes,
    invalid UTF-8, invalid JSON, and duplicate object keys at any depth.
    """
    if type(raw) is str:
        data = raw.encode("utf-8")
    elif type(raw) is bytes:
        data = raw
    else:
        raise TypeError(f"{label} must be str or bytes")
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the maximum message size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except (DuplicateKeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc
