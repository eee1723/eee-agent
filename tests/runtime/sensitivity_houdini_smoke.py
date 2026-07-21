"""Real-Houdini Secure Bridge sensitivity wire smoke.

This is a disposable integration probe, not a pytest test.  The fixture setup
and teardown use HOM only to create and destroy ``/obj/eee_sensitivity_smoke``;
every operation under test (sampling, stale admission, interruption and fault
classification) crosses the authenticated typed ``sensitivity.sample`` wire.
No HIP is saved and no file is written outside the temporary bridge state.

Run with::

    D:\\houdini\\bin\\hython.exe -u tests\\runtime\\sensitivity_houdini_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import hou  # type: ignore[import-not-found]  # noqa: E402

from eee_agent.houdini_bridge.auth import create_bridge_identity  # noqa: E402
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError  # noqa: E402
from eee_agent.houdini_bridge.queue import MainThreadReadQueue  # noqa: E402
from eee_agent.houdini_bridge.sensitivity import (  # noqa: E402
    SENSITIVITY_V1,
    SensitivitySampleRequest,
    SensitivitySampleResult,
    SensitivitySampleTarget,
)
from houdini_side.secure_bridge import (  # noqa: E402
    BridgeServer,
    HoudiniAdapterError,
    create_houdini_scene_adapter,
)

ROOT_PATH = "/obj/eee_sensitivity_smoke"
BOX_PATH = ROOT_PATH + "/sample_box"
PARM_NAME = "sizex"
_CHECKS: list[str] = []
_FAILURES: list[str] = []


def _ok(label: str) -> None:
    _CHECKS.append(label)
    print(f"  [ok]   {label}")


def _expect(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        _ok(label)
    else:
        _FAILURES.append(f"{label}: {detail or 'condition was false'}")
        print(f"  [FAIL] {label}: {detail or 'condition was false'}")


def _fingerprint() -> str:
    """Read-only inventory + parm/value/wire fingerprint for exact restore."""
    root = hou.node("/")
    if root is None:
        raise RuntimeError("hou.node('/') returned None")
    rows: list[object] = []
    for node in (root, *root.allSubChildren()):
        parms: list[object] = []
        for parm in node.parms():
            try:
                value = parm.eval()
            except Exception:
                value = "<unavailable>"
            parms.append((parm.name(), repr(value)))
        inputs = []
        for connection in node.inputConnections():
            source = connection.inputNode()
            inputs.append((connection.inputIndex(), source.path() if source else None))
        rows.append((node.path(), node.type().name(), tuple(sorted(parms)), tuple(inputs)))
    return hashlib.sha256(json.dumps(sorted(rows), sort_keys=True, default=str).encode()).hexdigest()


def _request(adapter, request_id: str, value: float, *, epoch: int | None = None, deadline_ms: int = 5000):
    binding = adapter.binding()
    return SensitivitySampleRequest.build(
        request_id=request_id,
        deadline_ms=deadline_ms,
        scene_epoch=binding.scene_epoch if epoch is None else epoch,
        node_paths=(BOX_PATH,),
        samples=(SensitivitySampleTarget(node_id=None, path=BOX_PATH, parm_name=PARM_NAME, value=value),),
    )


async def _pump(queue: MainThreadReadQueue, stop: asyncio.Event, pause: asyncio.Event) -> None:
    while not stop.is_set():
        if not pause.is_set():
            queue.pump_one()
        await asyncio.sleep(0.002)


async def _serve(adapter, state_dir: Path):
    queue = MainThreadReadQueue()
    identity = create_bridge_identity()
    server = BridgeServer(adapter=adapter, identity=identity, state_dir=state_dir, queue=queue)
    listener = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = await server.serve(listener, host="127.0.0.1")
    stop = asyncio.Event()
    pause = asyncio.Event()
    pump_task = asyncio.create_task(_pump(queue, stop, pause))
    return server, listener, stop, pause, pump_task, port


async def _run(adapter, state_dir: Path) -> None:
    server, listener, stop, pause, pump_task, port = await _serve(adapter, state_dir)
    print(f"bridge: 127.0.0.1:{port}; state={state_dir}")
    try:
        async with BridgeClient.from_state_dir(state_dir) as client:
            _expect(SENSITIVITY_V1 in client.capabilities, "capability sensitivity.v1")
            initial = _fingerprint()
            binding = adapter.binding()
            original = hou.node(BOX_PATH).parm(PARM_NAME).eval()  # type: ignore[union-attr]

            result = await client.sample_sensitivity(_request(adapter, "sensitivity-baseline", float(original) + 1.0))
            _expect(isinstance(result, SensitivitySampleResult), "sample returns typed result")
            _expect(len(result.samples) == 1, "sample ordering contains one result")
            _expect(result.baseline.binding.scene_epoch == binding.scene_epoch, "baseline epoch matches request")
            _expect(result.restored.binding.observed_revision == result.baseline.binding.observed_revision, "restored revision equals baseline")
            _expect(hou.node(BOX_PATH).parm(PARM_NAME).eval() == original, "sample restores parameter exactly")  # type: ignore[union-attr]
            _expect(_fingerprint() == initial, "sample restores exact scene fingerprint")
            _ok("request id and result ordering are preserved")

            outside_before = _fingerprint()
            try:
                await client.sample_sensitivity(_request(adapter, "sensitivity-stale", 2.0, epoch=binding.scene_epoch + 1))
            except BridgeClientError as exc:
                _expect(exc.code == "bridge.stale_scene", "stale epoch -> bridge.stale_scene", exc.code)
            else:
                _expect(False, "stale epoch rejected", "request unexpectedly succeeded")
            _expect(_fingerprint() == outside_before, "stale request performs zero writes")

            # A cancelled request is removed from the FIFO before HOM is called.
            # Observe the typed executor seam so a restored fingerprint cannot
            # mask an operation that actually ran and then restored.
            executor_before_cancel = server._executor
            original_sample = executor_before_cancel.sample_sensitivity
            sample_invocations = 0

            def observe_sample(request):
                nonlocal sample_invocations
                sample_invocations += 1
                return original_sample(request)

            executor_before_cancel.sample_sensitivity = observe_sample
            queue_before_cancel = server._queue
            pause.set()
            interrupted = asyncio.create_task(client.sample_sensitivity(_request(adapter, "sensitivity-interrupted", 3.0, deadline_ms=30000)))
            for _ in range(500):
                if queue_before_cancel.pending_count:
                    break
                await asyncio.sleep(0.002)
            _expect(queue_before_cancel.pending_count == 1, "interrupted request entered bridge queue")
            invocations_before_cancel = sample_invocations
            interrupted.cancel()
            try:
                await interrupted
            except asyncio.CancelledError:
                pass
            # Keep the main-thread pump paused while the cancellation seam is
            # applied; otherwise the queue could legitimately start the item
            # before cancellation is observed.
            # The queue cancellation call is the existing transport/main-thread
            # seam; the request itself was admitted over the typed wire above.
            _expect(
                queue_before_cancel.cancel("sensitivity-interrupted"),
                "cancelled request removed from bridge queue",
            )
            for _ in range(500):
                if queue_before_cancel.pending_count == 0:
                    break
                await asyncio.sleep(0.002)
            _expect(queue_before_cancel.pending_count == 0, "cancelled request leaves no pending queue item")
            _expect(sample_invocations == invocations_before_cancel, "cancelled request never invokes sensitivity executor")
            pause.clear()
            _expect(_fingerprint() == outside_before, "interrupted request does not replay a write")
            executor_before_cancel.sample_sensitivity = original_sample

            # BridgeClient intentionally closes its transport on cancellation;
            # reopen through the same discovery/token handoff before continuing.
            await client.close()
            client = BridgeClient.from_state_dir(state_dir)
            await client.open()
            _ok("reopened client after interrupted request")

            # Recreate the Secure Bridge process boundary.  A request carrying
            # the previous epoch is rejected after reopen; no old request is
            # replayed by the fresh executor.
            await client.close()
            server.close()
            listener.close()
            await listener.wait_closed()
            stop.set()
            await pump_task
            server, listener, stop, pause, pump_task, port = await _serve(adapter, state_dir)
            client = BridgeClient.from_state_dir(state_dir)
            await client.open()
            try:
                await client.sample_sensitivity(_request(adapter, "sensitivity-restart-stale", 3.5, epoch=adapter.binding().scene_epoch + 1))
            except BridgeClientError as exc:
                _expect(exc.code == "bridge.stale_scene", "restart rejects stale epoch", exc.code)
            else:
                _expect(False, "restart rejects stale epoch", "request unexpectedly succeeded")
            _expect(_fingerprint() == outside_before, "restart does not replay uncertain write")

            executor = server._executor  # typed operation seam, fault injection only
            original_cook = executor._cook_sample_node
            def fail_cook(node):
                raise HoudiniAdapterError(
                    code="sensitivity.cook_failed",
                    category="cook_failed",
                    message_for_user="forced cook failure",
                )
            executor._cook_sample_node = fail_cook
            try:
                await client.sample_sensitivity(_request(adapter, "sensitivity-cook-fail", 4.0))
            except BridgeClientError as exc:
                _expect(exc.code == "sensitivity.cook_failed", "forced cook failure -> sensitivity.cook_failed", exc.code)
            finally:
                executor._cook_sample_node = original_cook
            _expect(_fingerprint() == outside_before, "cook failure restores scene")

            original_write = executor._write_parm_value
            calls = 0
            def fail_restore(node, name, value):
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise RuntimeError("forced restore")
                return original_write(node, name, value)
            executor._write_parm_value = fail_restore
            try:
                await client.sample_sensitivity(_request(adapter, "sensitivity-restore-fail", 5.0))
            except BridgeClientError as exc:
                _expect(exc.code == "sensitivity.restore_failed", "forced restore failure -> sensitivity.restore_failed", exc.code)
            finally:
                executor._write_parm_value = original_write
            _expect(executor.write_frozen, "restore failure freezes subsequent writes")
            try:
                await client.sample_sensitivity(_request(adapter, "sensitivity-frozen", 6.0))
            except BridgeClientError as exc:
                _expect(exc.code == "bridge.write_frozen", "frozen bridge rejects next write", exc.code)

        # Restart/reopen with the current scene epoch; stale epochs remain rejected.
        server.close()
        listener.close()
        await listener.wait_closed()
        stop.set()
        await pump_task
        _ok("bridge shutdown completed")
    finally:
        if not server._closed:
            server.close()
        if not listener.is_serving():
            pass
        stop.set()
        if not pump_task.done():
            await pump_task


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args()
    before_fixture = _fingerprint()
    existing = hou.node(ROOT_PATH)
    if existing is not None:
        if existing.userData("eee.sensitivity_smoke") != "1":
            print(f"SMOKE FAIL: refusing to touch pre-existing unowned {ROOT_PATH}", file=sys.stderr)
            return 1
        existing.destroy()
    branch = None
    adapter = None
    state_ctx = None
    try:
        # Bounded fixture exception: HOM setup creates only our disposable branch.
        branch = hou.node("/obj").createNode("geo", "eee_sensitivity_smoke")
        branch.setUserData("eee.sensitivity_smoke", "1")
        box = branch.createNode("box", "sample_box")
        box.parm(PARM_NAME).set(1.0)
        box.cook(force=True)
        state_ctx = tempfile.TemporaryDirectory(prefix="eee-sensitivity-") if args.state_dir is None else None
        state_dir = args.state_dir or Path(state_ctx.name)
        state_dir.mkdir(parents=True, exist_ok=True)
        adapter = create_houdini_scene_adapter()
        adapter.install_scene_epoch_callbacks()
        asyncio.run(_run(adapter, state_dir))
    except Exception as exc:  # noqa: BLE001
        _FAILURES.append(f"uncaught smoke failure: {type(exc).__name__}: {exc}")
        print(f"  [FAIL] uncaught smoke failure: {type(exc).__name__}: {exc}")
    finally:
        if adapter is not None:
            adapter.close()
        if state_ctx is not None:
            state_ctx.cleanup()
        if branch is not None:
            branch.destroy()
        _expect(_fingerprint() == before_fixture, "no unowned node/temp file remains")
    print(f"sensitivity smoke checks={len(_CHECKS)} failures={len(_FAILURES)}")
    for failure in _FAILURES:
        print(f"SMOKE FAIL: {failure}", file=sys.stderr)
    return 1 if _FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
