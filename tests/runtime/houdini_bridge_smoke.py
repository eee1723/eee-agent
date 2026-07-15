"""Real-hython smoke for the secure read-only HoudiniBridge (Task 15-D).

Run inside Houdini 21.0.440 (hython or the Houdini Script Editor). The script
starts the loopback server with the REAL ``hou`` adapter, publishes the bridge
identity (``bridge.token`` + discovery) to ``--state-dir``, then connects as a
client and exercises the read-only contract end to end:

  * loopback-only binding + token handshake (correct and wrong token);
  * ``scene.query`` round-trip returning bounded DTO facts;
  * node path/type facts and selection parity (selection is a read-only UI
    state; the smoke selects an existing node as the "manual script" step);
  * stale-scene epoch rejection;
  * FIFO + deadline behaviour with no leftover queue items;
  * scene-epoch callback wiring on the real ``hou.hipFile``;
  * clean shutdown that removes the identity files and releases the port.

The smoke NEVER prints the token and NEVER creates/deletes/connects nodes,
sets parameters, or saves/exports/clears the HIP. It records a before/after
scene fingerprint and asserts the scene was not mutated. The detailed
load/clear/save epoch-increment logic is covered offline by
``tests/runtime/test_houdini_bridge_queue.py``; this smoke proves the real-hou
callback wiring and the end-to-end read-only path.

Usage (hython):

  & "D:\\houdini\\bin\\hython.exe" tests/runtime/houdini_bridge_smoke.py \\
      --state-dir "$env:TEMP\\eee-bridge-smoke" --host 127.0.0.1 --port 18811

Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make the repository importable when run directly with hython from the repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import hou  # noqa: E402 — provided by Houdini/hython at runtime

from eee_agent.houdini_bridge.auth import (  # noqa: E402
    BRIDGE_DISCOVERY_FILENAME,
    BRIDGE_TOKEN_FILENAME,
    create_bridge_identity,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError  # noqa: E402
from eee_agent.houdini_bridge.contracts import (  # noqa: E402
    PROTOCOL,
    BridgeRequest,
    SceneQueryResult,
)
from eee_agent.houdini_bridge.queue import MainThreadReadQueue  # noqa: E402
from houdini_side.secure_bridge import (  # noqa: E402
    BridgeServer,
    create_houdini_scene_adapter,
)

_HEADER_LEN = 4
_CHECKS: list[str] = []
_FAILURES: list[str] = []


def _ok(label: str) -> None:
    _CHECKS.append(label)
    print(f"  [ok]   {label}")


def _fail(label: str, detail: str) -> None:
    _FAILURES.append(f"{label}: {detail}")
    print(f"  [FAIL] {label}: {detail}")


def _expect(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        _ok(label)
    else:
        _fail(label, detail or "condition was false")


def _make_request(
    request_id: str,
    *,
    scene_epoch: int = 1,
    deadline_ms: int = 5000,
    include_selection: bool = True,
    node_paths: list[str] | None = None,
    include_geometry_stats: bool = True,
) -> BridgeRequest:
    return BridgeRequest.from_dict(
        {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": request_id,
            "operation": "scene.query",
            "deadline_ms": deadline_ms,
            "scene_epoch": scene_epoch,
            "payload": {
                "include_selection": include_selection,
                "node_paths": node_paths or [],
                "include_geometry_stats": include_geometry_stats,
            },
        }
    )


def _scene_fingerprint() -> str:
    """A read-only hash of the current scene's node inventory (no mutation)."""
    import hashlib

    try:
        root = hou.node("/obj")
        paths = []
        if root is not None:
            for child in root.children():
                paths.append(child.path())
        paths.sort()
    except Exception:  # noqa: BLE001 — keep the smoke read-only and resilient
        paths = []
    return hashlib.sha256(("|".join(paths)).encode("utf-8")).hexdigest()


async def _pump(queue: MainThreadReadQueue, stop: asyncio.Event, pause: asyncio.Event) -> None:
    """Cooperative main-thread stand-in for Houdini's main-thread pump callback."""
    while not stop.is_set():
        if not pause.is_set():
            queue.pump_one()
        await asyncio.sleep(0.002)


async def main(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Legacy unrestricted bridge must not be on this path.
    _expect("eee_agent.bridge" not in sys.modules, "legacy bridge not imported")

    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    before_fingerprint = _scene_fingerprint()
    binding0 = adapter.binding()
    print(f"scene: instance={binding0.instance_id} epoch={binding0.scene_epoch} "
          f"hip={binding0.hip_path}")

    # Epoch callback is wired into the real hou.hipFile (read-only check).
    _expect(len(hou.hipFile.eventCallbacks()) >= 1, "scene-epoch callback registered")

    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=state_dir, queue=queue
    )
    host = args.host
    aio = await asyncio.start_server(server.handle_connection, host, args.port)
    port = aio.sockets[0].getsockname()[1]
    server.publish_identity(host=host, port=port)
    print(f"server: bound {host}:{port}; identity published to {state_dir}")

    _expect((state_dir / BRIDGE_TOKEN_FILENAME).exists(), "bridge.token published")
    _expect(
        (state_dir / BRIDGE_DISCOVERY_FILENAME).exists(), "discovery published"
    )

    stop = asyncio.Event()
    pause = asyncio.Event()
    pump_task = asyncio.create_task(_pump(queue, stop, pause))

    try:
        # --- correct token handshake + round-trip -----------------------------
        client = BridgeClient.from_state_dir(state_dir)
        await client.open()
        _ok("token handshake succeeded (correct token)")

        result = await client.request(
            _make_request("req_q1", node_paths=["/obj"], include_geometry_stats=True)
        )
        _expect(isinstance(result, SceneQueryResult), "scene.query returns SceneQueryResult")
        _expect(
            any(n.path == "/obj" for n in result.nodes),
            "requested node /obj reported",
        )
        obj = next((n for n in result.nodes if n.path == "/obj"), None)
        if obj is not None:
            print(f"  node  /obj type={obj.node_type} parent={obj.parent_path} "
                  f"locked={obj.is_locked}")
        print(f"  binding epoch={result.binding.scene_epoch} "
              f"rev={result.binding.observed_revision[:24]}…")

        # --- selection parity (manual-script selection of an existing node) ---
        target = hou.node("/obj")
        if target is not None:
            target.setSelected(True)  # selection is a read-only UI state (#6 manual step)
            sel_result = await client.request(
                _make_request("req_sel", include_selection=True, node_paths=[])
            )
            selected_paths = [n.path for n in sel_result.selected_nodes]
            _expect(
                "/obj" in selected_paths,
                "selection parity: /obj appears in selected_nodes",
                f"got {selected_paths}",
            )
            target.setSelected(False)

        # --- stale scene epoch -> bridge.stale_scene --------------------------
        try:
            await client.request(_make_request("req_stale", scene_epoch=999))
            _fail("stale epoch rejected", "no error raised")
        except BridgeClientError as exc:
            _expect(
                exc.code == "bridge.stale_scene",
                "stale epoch -> bridge.stale_scene",
                f"got {exc.code}",
            )

        # --- FIFO: two sequential requests both succeed -----------------------
        r_a = await client.request(_make_request("req_fifo_a", node_paths=["/obj"]))
        r_b = await client.request(_make_request("req_fifo_b", node_paths=["/obj"]))
        _expect(
            isinstance(r_a, SceneQueryResult) and isinstance(r_b, SceneQueryResult),
            "FIFO: two sequential requests succeed",
        )

        # --- deadline: pump paused -> deadline_exceeded, no leftover item -----
        pause.set()
        try:
            await client.request(_make_request("req_dl", deadline_ms=1))
            _fail("deadline exceeded", "no error raised")
        except BridgeClientError as exc:
            _expect(
                exc.code == "bridge.deadline_exceeded",
                "deadline exceeded -> bridge.deadline_exceeded",
                f"got {exc.code}",
            )
        pause.clear()
        await asyncio.sleep(0.05)
        _expect(queue.pending_count == 0, "no leftover queue item after deadline")

        await client.close()
        _ok("client closed cleanly")

        # --- wrong token -> unauthorized --------------------------------------
        wrong = create_bridge_identity()
        bad_client = BridgeClient(host=host, port=port, identity=wrong)
        try:
            await bad_client.open()
            _fail("wrong token rejected", "no error raised")
        except BridgeClientError as exc:
            _expect(
                exc.code == "bridge.unauthorized",
                "wrong token -> bridge.unauthorized",
                f"got {exc.code}",
            )
            _expect(
                wrong.token not in str(exc) and wrong.token not in repr(exc),
                "wrong-token error carries no token",
            )

    finally:
        stop.set()
        pump_task.cancel()
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        server.close()
        # Houdini ships its own asyncio shim (houdini/python3.11libs/haio.py)
        # whose Server.close()/wait_closed() are not stdlib-compatible; tolerate
        # either flavour. The port is released when the short-lived smoke exits.
        try:
            aio.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(aio.wait_closed(), timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)

    # --- shutdown cleanup ---------------------------------------------------
    _expect(
        not (state_dir / BRIDGE_TOKEN_FILENAME).exists(),
        "shutdown removed bridge.token",
    )
    _expect(
        not (state_dir / BRIDGE_DISCOVERY_FILENAME).exists(),
        "shutdown removed discovery",
    )
    _expect(queue.pending_count == 0, "queue empty after shutdown")
    _expect(
        len(hou.hipFile.eventCallbacks()) == 0,
        "scene-epoch callback removed on close",
    )

    # --- scene not mutated --------------------------------------------------
    after_fingerprint = _scene_fingerprint()
    _expect(
        before_fingerprint == after_fingerprint,
        "scene not mutated (fingerprint unchanged)",
    )

    print(
        f"\nsmoke summary: {len(_CHECKS)} checks passed, "
        f"{len(_FAILURES)} failed"
    )
    return 0 if not _FAILURES else 1


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Secure HoudiniBridge read-only smoke")
    parser.add_argument("--state-dir", required=True, help="directory for bridge identity files")
    parser.add_argument("--host", default="127.0.0.1", help="loopback bind host (must be 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="bind port (0 = ephemeral)")
    ns = parser.parse_args(argv)
    if ns.host != "127.0.0.1":
        parser.error("--host must be 127.0.0.1 (loopback only)")
    return ns


if __name__ == "__main__":
    _args = _parse_args(sys.argv[1:])
    _rc = asyncio.run(main(_args))
    sys.exit(_rc)
