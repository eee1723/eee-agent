"""Persistent Runtime package for EEE Agent.

Adds an authenticated, loopback-only Runtime with durable sessions, runs,
event replay, and LangGraph checkpoints. This package grows task-by-task;
only the stable entry contracts are exported here.
"""

from eee_agent.runtime.paths import RuntimePaths

__all__ = ["RuntimePaths"]
