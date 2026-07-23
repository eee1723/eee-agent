"""Runtime CLI entry point: ``python -m eee_agent.runtime serve``.

Parses Runtime serve options, then runs the full production lifecycle in the
approved order and tears it down in reverse on any exit path (normal stop,
cancellation, or ``KeyboardInterrupt``). It composes the already-tested
Runtime components — :class:`RuntimePaths`, :class:`RuntimeLock`,
:class:`RuntimeService`, :class:`RuntimeWebSocketServer`, and the identity /
discovery helpers — and adds no persistence, protocol, or runner logic of its
own.

There is intentionally no test-runner switch here: production always wires the
real read-only runner via :func:`build_agent_runner`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from eee_agent.core import AgentException
from eee_agent.houdini_bridge.workspace_provider import (
    BridgeWorkspaceFactProvider,
)
from eee_agent.houdini_bridge.changeset_provider import BridgeChangeSetProvider
from eee_agent.houdini_bridge.read_only_provider import BridgeReadOnlyProvider
from eee_agent.modeling.catalog import houdini_21_minimal_catalog
from eee_agent.runtime.agent_runner import build_agent_runner
from eee_agent.runtime.auth import (
    RuntimeIdentity,
    cleanup_identity_files,
    create_identity,
    write_identity_files,
)
from eee_agent.runtime.lock import RuntimeLock
from eee_agent.runtime.paths import RuntimePaths
from eee_agent.runtime.protocol import PROTOCOL
from eee_agent.runtime.server import RuntimeWebSocketServer
from eee_agent.runtime.service import RuntimeService
from eee_agent.vision.router import VisionProvider

# The first implementation binds 127.0.0.1 exclusively (spec §3.3).
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 0
_DEFAULT_GRACEFUL_TIMEOUT = 10.0

# Signals that request a graceful shutdown (POSIX only; Windows falls back to
# KeyboardInterrupt / task cancellation — see :func:`_await_shutdown`).
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _positive_float(value: str) -> float:
    """argparse type: a finite, strictly positive float (the graceful timeout)."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid float value: {value!r}") from None
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError(
            f"--graceful-timeout must be a positive finite number, got {value!r}"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eee_agent.runtime",
        description=(
            "Persistent, authenticated, loopback-only EEE Agent Runtime. "
            f"Wire protocol is {PROTOCOL}."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser(
        "serve",
        help="serve the Runtime over a loopback WebSocket",
    )
    serve.add_argument(
        "--host",
        default=_DEFAULT_HOST,
        help=f"bind host (must be {_DEFAULT_HOST}; default {_DEFAULT_HOST})",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_PORT,
        help="bind port (0 = ephemeral; default 0)",
    )
    serve.add_argument(
        "--graceful-timeout",
        type=_positive_float,
        default=_DEFAULT_GRACEFUL_TIMEOUT,
        metavar="SECONDS",
        help=(
            "seconds to wait for an active read-only run to converge during "
            f"shutdown (default {_DEFAULT_GRACEFUL_TIMEOUT:g})"
        ),
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse Runtime CLI arguments without starting a server.

    Non-loopback hosts, unknown positionals/options, and non-positive graceful
    timeouts exit with a parser error (``SystemExit``) before any socket, lock,
    or database work. ``--help`` exits 0.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve" and args.host != _DEFAULT_HOST:
        parser.error(f"Runtime binds only to {_DEFAULT_HOST}; --host {args.host!r} is not allowed")
    return args


async def _await_shutdown() -> None:
    """Block until a shutdown is requested.

    On event loops that implement ``add_signal_handler`` (POSIX), SIGINT and
    SIGTERM set an event and this returns normally for a graceful unwind. On
    Windows (ProactorEventLoop has no ``add_signal_handler``) this awaits an
    event that is never set; ``asyncio.run``'s SIGINT handler cancels the main
    task (and Ctrl+C raises ``KeyboardInterrupt``), so the cancellation /
    keyboard interrupt unwinds the lifecycle's ``finally`` blocks instead.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    add_signal = getattr(loop, "add_signal_handler", None)
    installed = False
    if callable(add_signal):
        for sig in _SHUTDOWN_SIGNALS:
            try:
                add_signal(sig, stop.set)
                installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
    try:
        if installed:
            await stop.wait()
        else:
            # Windows path: wait forever until the task is cancelled.
            await asyncio.Event().wait()
    finally:
        remove_signal = getattr(loop, "remove_signal_handler", None)
        if installed and callable(remove_signal):
            for sig in _SHUTDOWN_SIGNALS:
                try:
                    remove_signal(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass


async def _serve_until_shutdown(
    server: RuntimeWebSocketServer,
    identity: RuntimeIdentity,
    state_dir: Path,
    *,
    host: str,
) -> None:
    """Bind ``server``, publish discovery, and serve until shutdown.

    Identity cleanup runs AFTER the server context exits (so server/client
    resources close first) and BEFORE the enclosing service context exits. The
    ``try/finally`` wrapping ``async with server:`` guarantees cleanup on a
    normal stop, cancellation, and ``KeyboardInterrupt``.
    """
    try:
        async with server:
            # The server has bound its actual (possibly ephemeral) port now;
            # publish discovery so clients can find and authenticate it.
            write_identity_files(
                identity, state_dir, host=host, port=server.port
            )
            await _await_shutdown()
    finally:
        cleanup_identity_files(identity, state_dir)


def _vision_settings() -> tuple[VisionProvider | None, float]:
    """Resolve one consistent provider/timeout snapshot from the environment."""
    from eee_agent.config import vision_config
    from eee_agent.vision.provider import build_vision_provider

    config = vision_config()
    if config is None:
        return None, 30.0
    return build_vision_provider(config), config.timeout_seconds


def _vision_provider() -> VisionProvider | None:
    """Build Vision only from its explicit provider-neutral configuration."""
    provider, _timeout = _vision_settings()
    return provider


async def async_main(argv: Sequence[str] | None = None) -> int:
    """Run the Runtime lifecycle until shutdown, then release every resource.

    Startup order: resolve/validate paths, create used directories, acquire the
    exclusive lock, open the service (database, reconciliation, checkpoints,
    runner), create and start the loopback server, then — only after the server
    has bound its real port — publish the identity/token/discovery files.

    Shutdown is the reverse and is guaranteed on normal exit, cancellation, and
    ``KeyboardInterrupt``: the server stops accepting and closes, the identity
    files are cleaned up (PID/nonce guarded), the service cancels any active run
    (waiting up to the configured graceful timeout) and closes the checkpoint
    manager and database, and the lock is released. The nested context managers
    express the dependency order, so unwinding always closes in reverse. A
    lock-contention :class:`AgentException` propagates before any
    service/server/identity is created.
    """
    args = parse_args(argv)
    if args.command != "serve":  # pragma: no cover - required subparser
        return 1

    paths = RuntimePaths.from_environment()
    paths.create_used_directories()
    vision_provider, vision_timeout_seconds = _vision_settings()

    with RuntimeLock(paths.lock_file):
        identity = create_identity()
        workspace_fact_provider = BridgeWorkspaceFactProvider(paths.state_dir)
        changeset_bridge_provider = BridgeChangeSetProvider(paths.state_dir)
        read_only_provider = BridgeReadOnlyProvider(paths.state_dir)
        async with RuntimeService.open(
            paths,
            runner_factory=lambda saver: build_agent_runner(
                saver, modeling=True
            ),
            graceful_timeout=args.graceful_timeout,
            changeset_bridge_provider=changeset_bridge_provider,
            workspace_fact_provider=workspace_fact_provider,
            modeling_catalog_provider=houdini_21_minimal_catalog,
            read_only_provider=read_only_provider,
            vision_provider=vision_provider,
            vision_timeout_seconds=vision_timeout_seconds,
        ) as service:
            server = RuntimeWebSocketServer(
                service, identity, host=args.host, port=args.port
            )
            await _serve_until_shutdown(
                server, identity, paths.state_dir, host=args.host
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Synchronous entry point used by ``python -m eee_agent.runtime``."""
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        # The lifecycle's finally blocks already ran during cancellation; report
        # the conventional SIGINT exit code.
        return 130
    except AgentException as exc:
        # A structured Runtime error (e.g. runtime.already_running): surface the
        # code/message to stderr without a traceback.
        print(
            f"[runtime] {exc.error.code}: {exc.error.message_for_user}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
