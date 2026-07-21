"""Persistent Runtime package for EEE Agent.

Adds an authenticated, loopback-only Runtime with durable sessions, runs,
event replay, and LangGraph checkpoints. This package grew task-by-task; only
the stable entry contracts are re-exported here. Internal helpers, repository
implementations, and database/migration details remain in their submodules and
are intentionally not re-exported.

The records/enums and :class:`RuntimePaths` are imported eagerly (they are
lightweight and were already public in Tasks 1–12). The heavier entry contracts
(service, server, lock, identity, runner) are resolved **lazily** via
``__getattr__`` (PEP 562): importing this package — or a light submodule such
as ``eee_agent.runtime.lock`` from a short-lived subprocess — must NOT pull
``deepagents``/``websockets``/``aiosqlite``. Accessing
``from eee_agent.runtime import RuntimeService`` resolves it on first use.

Run the Runtime with::

    python -m eee_agent.runtime serve
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
    "AgentRunner",
    "ApprovalSummary",
    "ChangeSetService",
    "DecisionResult",
    "EventCallback",
    "EventRecord",
    "ProposalResult",
    "RetentionClass",
    "RunRecord",
    "RunStatus",
    "RuntimeIdentity",
    "RuntimeLock",
    "RuntimePaths",
    "RuntimeService",
    "RuntimeWebSocketServer",
    "RunnerCompleted",
    "RunnerEvent",
    "RunnerFactory",
    "SessionRecord",
    "SessionSnapshot",
    "SessionStatus",
    "build_agent_runner",
    "create_identity",
    "require_transition",
]

# Maps a public (lazy) name to the submodule that defines it. Resolved on first
# access via __getattr__ so a bare package import stays lightweight.
_LAZY: dict[str, str] = {
    "AgentRunner": "eee_agent.runtime.agent_runner",
    "RunnerCompleted": "eee_agent.runtime.agent_runner",
    "RunnerEvent": "eee_agent.runtime.agent_runner",
    "RunnerFactory": "eee_agent.runtime.agent_runner",
    "build_agent_runner": "eee_agent.runtime.agent_runner",
    "RuntimeIdentity": "eee_agent.runtime.auth",
    "create_identity": "eee_agent.runtime.auth",
    "RuntimeLock": "eee_agent.runtime.lock",
    "RuntimeService": "eee_agent.runtime.service",
    "SessionSnapshot": "eee_agent.runtime.service",
    "EventCallback": "eee_agent.runtime.service",
    "RuntimeWebSocketServer": "eee_agent.runtime.server",
    # Task 16-B2a: stable ChangeSet approval records/service re-exported here so
    # the public Runtime surface keeps one import path. They remain defined in
    # the changesets package and are resolved lazily.
    "ChangeSetService": "eee_agent.changesets.service",
    "ApprovalSummary": "eee_agent.changesets.service",
    "ProposalResult": "eee_agent.changesets.repository",
    "DecisionResult": "eee_agent.changesets.repository",
}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # cache so subsequent accesses skip the import
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
