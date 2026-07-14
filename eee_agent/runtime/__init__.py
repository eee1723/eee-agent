"""Persistent Runtime package for EEE Agent.

Adds an authenticated, loopback-only Runtime with durable sessions, runs,
event replay, and LangGraph checkpoints. This package grows task-by-task;
only the stable entry contracts are exported here.
"""

from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionRecord,
    SessionStatus,
    require_transition,
)
from eee_agent.runtime.paths import RuntimePaths

__all__ = [
    "EventRecord",
    "RetentionClass",
    "RunRecord",
    "RunStatus",
    "RuntimePaths",
    "SessionRecord",
    "SessionStatus",
    "require_transition",
]
