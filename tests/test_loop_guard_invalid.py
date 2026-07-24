"""Tests for the loop guard's awareness of invalid tool calls (M3 fix).

M3: the guard previously iterated only ``msg.tool_calls`` and ignored
``msg.invalid_tool_calls``, so a model retry-storm of the SAME malformed
scratch_build call never tripped the guard. Now both are scanned.
"""
from __future__ import annotations

from types import SimpleNamespace

from eee_agent.loop_guard import _recent_calls


def _ai_invalid(name, args):
    """An AIMessage carrying a malformed (invalid) tool call."""
    return SimpleNamespace(
        tool_calls=[],
        invalid_tool_calls=[{"name": name, "args": args, "id": "x", "type": "invalid_tool_call"}],
    )


def _ai_valid(name, args):
    return SimpleNamespace(
        tool_calls=[{"name": name, "args": args, "id": "x", "type": "tool_call"}],
        invalid_tool_calls=[],
    )


def test_invalid_scratch_build_calls_are_counted():
    """Repeated identical invalid scratch_build calls register as retries."""
    # Same malformed args repeated 4 times.
    msgs = [_ai_invalid("scratch_build", '{"operations":[{"kind":"set_parm","value":BAD}]}') for _ in range(4)]
    sigs = _recent_calls(msgs, limit=16)
    assert len(sigs) == 4
    # All share the same signature (same collapsed args string).
    assert all(s == sigs[0] for s in sigs)


def test_distinct_invalid_calls_are_distinct():
    """Different malformed args are distinct signatures (not flagged as thrash)."""
    msgs = [
        _ai_invalid("scratch_build", '{"operations":[{"kind":"create_node"}]}'),
        _ai_invalid("scratch_build", '{"operations":[{"kind":"set_parm"}]}'),
    ]
    sigs = _recent_calls(msgs, limit=16)
    assert len(sigs) == 2
    assert sigs[0] != sigs[1]


def test_mix_of_valid_and_invalid_calls_counted():
    msgs = [
        _ai_valid("scratch_build", {"operations": [{"kind": "create_node", "node_name": "a"}]}),
        _ai_invalid("scratch_build", '{"operations":'),  # truncated args string
    ]
    sigs = _recent_calls(msgs, limit=16)
    assert len(sigs) == 2


def test_readonly_invalid_calls_ignored():
    """Only MUTATIVE tools are watched; invalid calls to read-only tools don't count."""
    msgs = [_ai_invalid("scene_status", "bad") for _ in range(5)]
    assert _recent_calls(msgs, limit=16) == []


def test_limit_window_applies_to_invalid_too():
    msgs = [_ai_invalid("scratch_commit", '{"a":1}') for _ in range(10)]
    sigs = _recent_calls(msgs, limit=3)
    assert len(sigs) == 3
