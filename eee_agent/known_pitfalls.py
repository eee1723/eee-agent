"""Dynamic registry of node types / operations where the model historically
makes mistakes, so the system prompt can require a knowledge-base check ONLY
for those — instead of forcing a KB query before every node creation (which
wasted ~38% of tool calls in profiling).

Design intent (per the team's direction): do NOT hard-code a static allow-list
of "common nodes that don't need a check". Instead ship an empty default
(trust the model's common sense) and let the operator grow the list from real
observed failures. When you notice the model repeatedly getting a node's parm
names wrong in production, add that node type to ``EEE_PITFALL_NODES`` and
restart — no code change, no prompt rewrite.

Override via environment:
    EEE_PITFALL_NODES=copytopoints2,polyextrude2,sweep2

Entries match case-insensitively against the ``node_type`` the model passes to
scratch_build's ``create_node`` op. A bare prefix match is used so an entry
like "copytopoints" also covers versioned variants "copytopoints2".
"""
from __future__ import annotations

import os

# Module-level cache of the parsed set. Read once at import; tests that mutate
# the env var call ``_reload()`` (or import fresh). Production reads it once at
# startup via build_system_prompt().
_pitfalls: frozenset[str] = frozenset()


def _reload() -> frozenset[str]:
    """Parse EEE_PITFALL_NODES into a lowercased frozenset. Empty when unset."""
    global _pitfalls
    raw = os.getenv("EEE_PITFALL_NODES", "").strip()
    if not raw:
        _pitfalls = frozenset()
        return _pitfalls
    entries = {part.strip().lower() for part in raw.split(",") if part.strip()}
    _pitfalls = frozenset(entries)
    return _pitfalls


# Populate on import.
_reload()


def known_pitfalls() -> tuple[str, ...]:
    """Return the current pitfall node types as a sorted tuple (for display)."""
    return tuple(sorted(_pitfalls))


def requires_knowledge_check(node_type: object) -> bool:
    """True when ``node_type`` is a registered pitfall (needs a KB query first).

    Matches case-insensitively and by prefix: a registered ``"copytopoints"``
    covers ``"copytopoints2"`` too, so the operator doesn't have to enumerate
    every versioned variant. Returns False for an empty registry (the default):
    trust the model's common sense unless told otherwise."""
    if not _pitfalls:
        return False
    if not isinstance(node_type, str):
        return False
    nt = node_type.strip().lower()
    if not nt:
        return False
    return any(nt == p or nt.startswith(p) for p in _pitfalls)


def any_pitfalls_registered() -> bool:
    """Whether any pitfall is currently registered (drives prompt wording)."""
    return bool(_pitfalls)
