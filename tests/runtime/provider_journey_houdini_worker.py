"""Hython-side Secure Bridge worker for the provider-journey adapter.

This module is launched ONLY by ``tests/runtime/provider_journey.py`` with
hython (``hou`` is unavailable under pytest/the venv). It mirrors the proven
``tests/runtime/houdini_bridge_smoke.py`` startup pattern: a real
:class:`HoudiniSceneAdapter`, one :class:`MainThreadReadQueue` pumped by a
cooperative asyncio task on Houdini's main thread (hython has no UI event
loop, so ``hou.ui.addEventLoopCallback`` never fires), and a
:class:`BridgeServer` that publishes the bridge identity (``bridge.token`` +
discovery) into the Runtime state directory for the production
``BridgeWorkspaceFactProvider`` / ``BridgeChangeSetProvider`` discovery path.

Lifecycle: optionally load the disposable HIP, serve until the adapter writes
the stop file, then destroy every ``/obj`` child that did not exist at
startup (bounded scene cleanup), stop the server (which removes the identity
files), write a tiny cleanup receipt, and exit 0. It never saves, exports, or
clears the HIP and never prints the bridge token or any scene content beyond
bounded counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_HOST = "127.0.0.1"
_STOP_POLL_SECONDS = 0.1
_PUMP_IDLE_SECONDS = 0.002


def _die(message: str) -> None:
    print(f"PROVIDER-JOURNEY WORKER FAIL: {message}", file=sys.stderr)
    sys.exit(1)


async def _serve(args: argparse.Namespace) -> int:
    import hou

    from eee_agent.houdini_bridge.auth import create_bridge_identity
    from eee_agent.houdini_bridge.queue import MainThreadReadQueue
    from houdini_side.secure_bridge import (
        BridgeServer,
        create_houdini_scene_adapter,
    )

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    stop_file = Path(args.stop_file)
    cleanup_file = Path(args.cleanup_file)

    hip_path = Path(args.hip_path)
    if hip_path.is_file():
        hou.hipFile.load(str(hip_path))

    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter,
        identity=create_bridge_identity(),
        state_dir=state_dir,
        queue=queue,
    )
    listener = await asyncio.start_server(server.handle_connection, _HOST, 0)
    await server.serve(listener, host=_HOST)
    print("worker: secure bridge identity published", flush=True)

    obj = hou.node("/obj")
    initial_children = (
        {child.path() for child in obj.children()} if obj is not None else set()
    )

    stop = asyncio.Event()

    async def pump() -> None:
        while not stop.is_set():
            queue.pump_one()
            await asyncio.sleep(_PUMP_IDLE_SECONDS)

    pump_task = asyncio.create_task(pump())
    try:
        while not stop_file.is_file():
            await asyncio.sleep(_STOP_POLL_SECONDS)
    finally:
        stop.set()
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # Bounded scene cleanup: destroy only nodes created after startup.
    removed = 0
    if obj is not None:
        for child in list(obj.children()):
            if child.path() not in initial_children:
                child.destroy()
                removed += 1
    # stop() owns the full shutdown: close the listener, drain the queue,
    # close writers, remove the identity files and the epoch callback.
    await server.stop()
    cleanup_file.write_text(
        json.dumps({"removed": removed}), encoding="utf-8"
    )
    print(f"worker: scene cleanup removed {removed} node(s)", flush=True)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provider-journey Secure Bridge worker (hython only)"
    )
    parser.add_argument(
        "--state-dir", required=True, help="Runtime state dir for bridge identity"
    )
    parser.add_argument(
        "--hip-path", required=True, help="disposable HIP to load when it exists"
    )
    parser.add_argument(
        "--stop-file", required=True, help="touch file that requests shutdown"
    )
    parser.add_argument(
        "--cleanup-file", required=True, help="bounded cleanup receipt output"
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        import hou  # noqa: F401 — only available inside hython
    except ImportError:
        _die("hou is not importable; run this worker with hython, not python.")
    try:
        return asyncio.run(_serve(args))
    except Exception as exc:  # noqa: BLE001 — bounded, leak-free failure
        _die(f"unhandled worker failure: {type(exc).__name__}")
    return 1  # pragma: no cover - _die exits


if __name__ == "__main__":
    if "PYTEST" in "".join(sys.argv).upper() or "pytest" in sys.argv[0]:
        _die("this worker runs under hython, not pytest")
    sys.exit(main(sys.argv[1:]))
