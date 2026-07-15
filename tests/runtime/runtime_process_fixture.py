"""Test-only Runtime process fixture (spawned as an independent process).

Run as::

    python -m tests.runtime.runtime_process_fixture [complete|block]

It opens the REAL ``RuntimeService`` and ``RuntimeWebSocketServer`` with a
deterministic fake ``RunnerFactory`` (no live LLM, no Houdini), publishes the
discovery/token files for the parent test to read, and blocks until the parent
terminates the process. This deliberately duplicates the production lifecycle
(see ``eee_agent/runtime/__main__.py``) instead of injecting a fake runner into
production code — there is no test-runner switch in the Runtime package.

Modes:

* ``complete`` (default): the fake runner yields a text delta, a model.completed
  event, and a terminal ``RunnerCompleted``. A started run reaches Completed.
* ``block``: the fake runner never yields a terminal event, so a started run
  stays non-terminal (Planning). Hard-killing the process leaves an interrupted
  run for restart-reconciliation coverage.
"""

from __future__ import annotations

import asyncio
import sys

from eee_agent.runtime.agent_runner import RunnerCompleted, RunnerEvent
from eee_agent.runtime.auth import (
    cleanup_identity_files,
    create_identity,
    write_identity_files,
)
from eee_agent.runtime.lock import RuntimeLock
from eee_agent.runtime.models import RetentionClass
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.server import RuntimeWebSocketServer
from eee_agent.runtime.service import RuntimeService

_USAGE = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
_BIND_HOST = "127.0.0.1"


class _CompletingFakeRunner:
    """Deterministic fake runner: one text delta, completion, then done."""

    async def stream(self, *, session_id: str, user_input: str):
        yield RunnerEvent(
            "model.text_delta",
            {"text": "ok"},
            RetentionClass.OPERATIONAL,
        )
        yield RunnerEvent(
            "model.completed",
            {"usage": dict(_USAGE)},
            RetentionClass.DURABLE,
        )
        yield RunnerCompleted(
            final_response=f"echo:{user_input}", usage=dict(_USAGE)
        )


class _BlockingFakeRunner:
    """Deterministic fake runner that never completes.

    ``stream`` blocks forever, so a started run stays non-terminal until the
    process is killed. The unreachable ``yield`` makes this an async generator.
    """

    async def stream(self, *, session_id: str, user_input: str):
        await asyncio.Event().wait()  # never returns
        yield  # pragma: no cover


def _runner_factory(mode: str):
    def factory(_checkpointer):
        if mode == "block":
            return _BlockingFakeRunner()
        return _CompletingFakeRunner()

    return factory


async def _serve(mode: str) -> None:
    paths = RuntimePaths.from_environment()
    paths.create_used_directories()
    with RuntimeLock(paths.lock_file):
        identity = create_identity()
        async with RuntimeService.open(
            paths, runner_factory=_runner_factory(mode)
        ) as service:
            server = RuntimeWebSocketServer(
                service, identity, host=_BIND_HOST, port=0
            )
            async with server:
                # The server has bound its ephemeral port; publish discovery so
                # the parent can read it and authenticate.
                write_identity_files(
                    identity,
                    paths.state_dir,
                    host=_BIND_HOST,
                    port=server.port,
                )
                try:
                    # Block until the parent hard-kills this process. No signal
                    # handling is required: the parent owns the lifecycle and
                    # the temporary Runtime home.
                    await asyncio.Event().wait()
                finally:
                    cleanup_identity_files(identity, paths.state_dir)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "complete"
    if mode not in ("complete", "block"):
        print(f"[fixture] unknown mode: {mode!r}", file=sys.stderr)
        return 2
    try:
        asyncio.run(_serve(mode))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — surface to stderr for the parent
        print(f"[fixture] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
