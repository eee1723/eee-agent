"""Task 19-A disposable real-Houdini capture smoke (run with hython, NOT pytest).

Exercises the typed ``capture.capture`` operation through the REAL Secure
Bridge against the live ``hou`` module: it creates one owned
``/obj/eee_capture_smoke`` branch (a geo node with a small box), captures it
through the real client/server wire into a temp artifacts directory, and
verifies the Task 19-A contract:

  * the returned reference sha256 hash-identically matches the delivered file
    bytes (the same bytes any later viewer/vision input must read);
  * the delivered PNG is 1280x960 (IHDR), written next to no leftover
    temp render directory;
  * the bounded framing report sits inside the deterministic acceptance band
    (all four edges >= 6% margin, longest axis 72%..84%, center offset <= 3%,
    at most two adjustments);
  * the owned temp camera/ROP scope is destroyed (no ``eee_capture_*`` nodes
    remain under /obj or /out);
  * the scene is not mutated beyond the owned smoke branch;
  * a stale scene epoch fails closed with ``bridge.stale_scene`` and writes
    no artifact.

The smoke destroys its owned branch on exit and never saves, loads, clears,
or exports the user's HIP.

Run (detected hython)::

    "<Houdini>/bin/hython.exe" tests/runtime/capture_houdini_smoke.py

Exit code 0 on success; non-zero with a message on any failure. The module is
imported by hython only (``hou`` is unavailable under pytest/the venv), so it
guards on ``hou`` and is excluded from offline collection by name/convention.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE_PACKAGES = _REPO_ROOT / ".venv" / "Lib" / "site-packages"
for _path in (_REPO_ROOT, _VENV_SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

try:
    import hou  # noqa: E402 — provided by Houdini/hython at runtime
except ImportError:
    print("SMOKE SKIP: hou is unavailable (run with hython)", file=sys.stderr)
    sys.exit(2)

from eee_agent.houdini_bridge.auth import create_bridge_identity  # noqa: E402
from eee_agent.houdini_bridge.capture import (  # noqa: E402
    CAPTURE_V1,
    CaptureRequest,
    CaptureResult,
)
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError  # noqa: E402
from eee_agent.houdini_bridge.queue import MainThreadReadQueue  # noqa: E402
from houdini_side.secure_bridge import (  # noqa: E402
    BridgeServer,
    create_houdini_scene_adapter,
)

_SMOKE_ROOT = "/obj/eee_capture_smoke"
_ART = "art_" + "0" * 32
_FAILURES: list[str] = []
_CHECKS = 0


def _die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _ok(label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    print(f"  [ok]   {label}")


def _fail(label: str, detail: str) -> None:
    _FAILURES.append(f"{label}: {detail}")
    print(f"  [FAIL] {label}: {detail}")


def _expect(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        _ok(label)
    else:
        _fail(label, detail or "condition was false")


def _fingerprint() -> str:
    """Read-only inventory hash of /obj + /out outside the smoke branch."""
    paths: list[str] = []
    for root_path in ("/obj", "/out"):
        root = hou.node(root_path)
        if root is None:
            continue
        for node in (root, *root.allSubChildren()):
            if node.path().startswith(_SMOKE_ROOT):
                continue
            paths.append(f"{node.path()}|{node.type().name()}")
    paths.sort()
    return hashlib.sha256("\n".join(paths).encode("utf-8")).hexdigest()


async def _pump(queue: MainThreadReadQueue, stop: asyncio.Event) -> None:
    """Cooperative main-thread stand-in for Houdini's main-thread pump."""
    while not stop.is_set():
        queue.pump_one()
        await asyncio.sleep(0.002)


async def _run() -> None:
    workspace = Path(tempfile.mkdtemp(prefix="eee-capture-smoke-"))
    state_dir = workspace / "state"
    state_dir.mkdir(parents=True)
    target_dir = workspace / "artifacts" / "ses" / "run"
    target_dir.mkdir(parents=True)

    hip_name = hou.hipFile.name()
    print(f"scene: hip={hip_name}")
    # ---- owned smoke branch ------------------------------------------------
    obj = hou.node("/obj")
    if obj is None:
        _die("hou.node('/obj') is unavailable")
    root = obj.createNode("geo", "eee_capture_smoke")
    for child in root.children():
        child.destroy()
    box = root.createNode("box", "box1")
    box.parm("sizex").set(2.0)
    box.parm("sizey").set(1.0)
    box.parm("sizez").set(1.0)
    box.cook(force=True)
    errors = box.errors()
    if errors:
        _die(f"smoke box did not cook: {errors[0]}")
    _ok("owned smoke branch created and cooked")

    adapter = create_houdini_scene_adapter()
    adapter.install_scene_epoch_callbacks()
    binding = adapter.binding()
    before = _fingerprint()

    identity = create_bridge_identity()
    queue = MainThreadReadQueue()
    server = BridgeServer(
        adapter=adapter, identity=identity, state_dir=state_dir, queue=queue
    )
    aio = await asyncio.start_server(server.handle_connection, "127.0.0.1", 0)
    port = await server.serve(aio, host="127.0.0.1")
    print(f"server: bound 127.0.0.1:{port}")
    stop = asyncio.Event()
    pump_task = asyncio.create_task(_pump(queue, stop))
    try:
        client = BridgeClient.from_state_dir(state_dir)
        await client.open()
        try:
            _expect(
                CAPTURE_V1 in client.capabilities,
                "capture.v1 advertised by the real bridge",
                f"got {client.capabilities}",
            )
            request = CaptureRequest.build(
                request_id="req_capture",
                deadline_ms=30000,
                scene_epoch=binding.scene_epoch,
                node_paths=[_SMOKE_ROOT],
                target_dir=str(target_dir),
                artifact_id=_ART,
            )
            result = await client.capture(request)
            _expect(isinstance(result, CaptureResult), "capture returns CaptureResult")

            # ---- byte identity: the reference matches the delivered file ----
            delivered = target_dir / f"{_ART}.png"
            _expect(delivered.is_file(), "capture file delivered to target dir")
            payload = delivered.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            _expect(
                result.sha256 == digest,
                "reference sha256 hash-identically matches delivered bytes",
                f"wire={result.sha256} file={digest}",
            )
            _expect(
                result.size_bytes == len(payload),
                "reference size matches delivered bytes",
            )
            _expect(
                result.relative_path == f"{_ART}.png",
                "reference relative_path is the single delivered component",
            )
            _expect(result.media_type == "image/png", "media type is image/png")
            _expect(result.artifact_id == _ART, "artifact id echoed exactly")
            _expect(
                not (target_dir / f".tmp_{_ART}").exists(),
                "no leftover temp render directory after the atomic rename",
            )

            # ---- deterministic output shape ---------------------------------
            _expect(payload[:8] == b"\x89PNG\r\n\x1a\n", "delivered file is a PNG")
            width, height = struct.unpack(">II", payload[16:24])
            _expect(
                (width, height) == (1280, 960),
                "capture is 1280x960",
                f"got {width}x{height}",
            )

            # ---- framing acceptance band ------------------------------------
            framing = result.framing
            _expect(
                0 <= framing.adjustments_used <= 2,
                "framing used at most two adjustments",
                f"got {framing.adjustments_used}",
            )
            margins = (
                framing.margin_left,
                framing.margin_right,
                framing.margin_bottom,
                framing.margin_top,
            )
            _expect(
                all(margin >= 0.06 for margin in margins),
                "all four edges keep >= 6% margin",
                f"got {margins}",
            )
            _expect(
                0.72 <= framing.longest_axis_ratio <= 0.84,
                "longest axis occupies 72%..84% of frame",
                f"got {framing.longest_axis_ratio}",
            )
            _expect(
                framing.center_offset <= 0.03,
                "center offset within 3%",
                f"got {framing.center_offset}",
            )

            # ---- temp scope cleanup + scene untouched -----------------------
            _expect(
                hou.node("/obj/eee_capture_" + _ART[4:]) is None
                and hou.node("/out/eee_capture_" + _ART[4:]) is None,
                "owned temp camera/ROP scope destroyed",
            )
            _expect(
                _fingerprint() == before,
                "scene not mutated beyond the owned smoke branch",
            )

            # ---- explicit failure: stale epoch writes no artifact -----------
            try:
                await client.capture(
                    CaptureRequest.build(
                        request_id="req_capture_stale",
                        deadline_ms=5000,
                        scene_epoch=binding.scene_epoch + 1,
                        node_paths=[_SMOKE_ROOT],
                        target_dir=str(target_dir),
                        artifact_id="art_" + "1" * 32,
                    )
                )
                _fail("stale epoch rejected", "no error raised")
            except BridgeClientError as exc:
                _expect(
                    exc.code == "bridge.stale_scene",
                    "stale epoch -> bridge.stale_scene",
                    f"got {exc.code}",
                )
            _expect(
                not (target_dir / ("art_" + "1" * 32 + ".png")).exists(),
                "stale capture wrote no artifact",
            )
        finally:
            await client.close()
    finally:
        stop.set()
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        await server.stop()
        root.destroy()
        _ok("owned smoke branch destroyed")

    if _FAILURES:
        _die(f"{len(_FAILURES)} checks failed: {_FAILURES[0]}")
    print(f"SMOKE OK: {_CHECKS} checks passed, 0 failed")


def main() -> int:
    try:
        asyncio.run(_run())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — smoke reports, never traces deep
        _die(f"unexpected error: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
