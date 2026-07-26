"""Deterministic real-Houdini Secure Bridge journey for scratch modeling.

This is the L2 layer: a hython worker hosts the real Secure Bridge while this
venv-side driver uses the production Bridge providers and ScratchCoordinator.
No RuntimeService, LangGraph, LLM, or credentials are involved.  L1
``scratch_houdini_smoke.py`` already tests the executor directly; this script
adds discovery, authentication, capability, transport, provider, and cleanup
coverage without model nondeterminism.

Usage::

    python tests/runtime/scratch_bridge_houdini_journey.py \
      --hython "C:/.../hython.exe" --evidence journey.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace


_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE_PACKAGES = _ROOT / ".venv" / "Lib" / "site-packages"
for _path in (_ROOT, _VENV_SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

_WORKER = Path(__file__).with_name("provider_journey_houdini_worker.py")
_TOKEN = "bridge.token"
_DISCOVERY = "bridge.discovery.json"
_STOP = "scratch_bridge.stop"
_CLEANUP = "scratch_bridge.cleanup.json"
_MAX_EVIDENCE_BYTES = 16 * 1024


class _NoopKnowledge:
    def search(self, query: str, *, limit: int = 5) -> dict[str, object]:
        return {"ok": True, "results": []}

    def get(
        self, entity_id: str, *, max_body_bytes: int = 8_000
    ) -> dict[str, object]:
        return {"ok": True, "entity_id": entity_id}


def _die(message: str) -> None:
    raise RuntimeError(f"SCRATCH BRIDGE JOURNEY FAIL: {message}")


def _expect(condition: bool, message: str) -> None:
    if not condition:
        _die(message)


def _scrubbed_worker_env(hfs: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper.startswith("EEE_LLM_"):
            continue
        if any(tag in upper for tag in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")):
            continue
        env[key] = value
    env["HOUDINI_PACKAGE_SKIP"] = "1"
    env["HOUDINI_NO_ENV_FILE"] = "1"
    env["HOUDINI_PATH"] = (
        f"{hfs.as_posix()}/packages/apex;"
        f"{hfs.as_posix()}/packages/kinefx;&"
    )
    return env


def _start_worker(
    *,
    hython: Path,
    state_dir: Path,
    hip_path: Path,
    stop_file: Path,
    cleanup_file: Path,
    worker_log: Path,
) -> subprocess.Popen:
    log = worker_log.open("w", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            [
                str(hython),
                str(_WORKER),
                "--state-dir",
                str(state_dir),
                "--hip-path",
                str(hip_path),
                "--stop-file",
                str(stop_file),
                "--cleanup-file",
                str(cleanup_file),
            ],
            cwd=str(_ROOT),
            env=_scrubbed_worker_env(hython.parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
    except OSError:
        log.close()
        raise
    process._eee_log = log  # type: ignore[attr-defined]
    return process


def _close_log(process: subprocess.Popen) -> None:
    log = getattr(process, "_eee_log", None)
    if log is not None:
        try:
            log.close()
        except OSError:
            pass
        process._eee_log = None  # type: ignore[attr-defined]


def _wait_for_bridge(state_dir: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"hython worker exited before bridge discovery: {process.returncode}"
            )
        if (state_dir / _TOKEN).is_file() and (state_dir / _DISCOVERY).is_file():
            return
        time.sleep(0.2)
    raise RuntimeError("bridge discovery was not published within 120s")


def _stop_worker(
    process: subprocess.Popen,
    *,
    stop_file: Path,
    cleanup_file: Path,
) -> dict[str, object]:
    stop_file.write_text("stop\n", encoding="utf-8")
    try:
        return_code = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        process.kill()
        raise RuntimeError("hython worker did not stop within 120s") from None
    finally:
        _close_log(process)
    if return_code != 0:
        raise RuntimeError(f"hython worker exited with code {return_code}")
    try:
        raw = json.loads(cleanup_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("worker cleanup evidence is missing or malformed") from exc
    if type(raw) is not dict or type(raw.get("removed")) is not int:
        raise RuntimeError("worker cleanup evidence has an invalid shape")
    return raw


async def _journey(state_dir: Path, token: str) -> dict[str, object]:
    from eee_agent.houdini_bridge.changeset_provider import BridgeChangeSetProvider
    from eee_agent.houdini_bridge.read_only_provider import BridgeReadOnlyProvider
    from eee_agent.modeling.scratch_coordinator import (
        ScratchCoordinator,
        ScratchSessionContext,
    )

    provider = BridgeChangeSetProvider(state_dir, deadline_ms=30_000)
    read_only = BridgeReadOnlyProvider(state_dir)
    status = await read_only.scene_status()
    _expect(status.get("ok") is True, "scene_status did not complete")
    capabilities = status.get("capabilities")
    _expect(
        isinstance(capabilities, list) and "scratch.v1" in capabilities,
        "scratch.v1 capability was not advertised",
    )

    sandbox_id = f"bridge_{token}"
    coordinator = ScratchCoordinator(
        ScratchSessionContext(provider=provider, sandbox_id=sandbox_id)
    )
    build = await coordinator.build(
        operations=[
            {"kind": "create_node", "node_name": "box1", "node_type": "box"},
            {"kind": "set_parm", "node_name": "box1", "parm": "sizex", "value": 2.0},
            {"kind": "set_parm", "node_name": "box1", "parm": "sizey", "value": 1.0},
            {"kind": "set_parm", "node_name": "box1", "parm": "sizez", "value": 3.0},
        ]
    )
    _expect(build.get("ok") is True, f"Bridge build failed: {build}")
    geometry = build.get("geometry")
    _expect(
        isinstance(geometry, dict) and geometry.get("point_count", 0) > 0,
        "Bridge build returned empty geometry",
    )
    from eee_agent.runtime.agent_context import RuntimeToolContext
    from eee_agent.runtime.sketch_tools import verify_geometry

    verified = await verify_geometry.coroutine(
        str(build["output_node"]),
        {
            "min_verts": 8,
            "max_verts": 8,
            "min_faces": 6,
            "max_faces": 6,
        },
        SimpleNamespace(
            context=RuntimeToolContext(
                read_only=read_only,
                knowledge=_NoopKnowledge(),
            )
        ),
    )
    _expect(
        verified.get("ok") is True,
        f"production verify_geometry rejected Bridge stats: {verified}",
    )

    final_name = f"eee_bridge_asset_{token}"
    commit = await coordinator.commit(
        target_parent_path="/obj",
        target_name=final_name,
        skip_structure_check=True,
    )
    _expect(commit.get("ok") is True and commit.get("committed") is True, f"Bridge commit failed: {commit}")
    final_path = commit.get("final_path")
    _expect(type(final_path) is str, "commit final_path is missing")
    final_nodes = await read_only.query_scene([final_path])
    _expect(
        final_nodes.get("ok") is True and final_nodes.get("node_count") == 1,
        "final container query failed",
    )
    final_stats = await read_only.geometry_stats(f"{final_path}/box1")
    _expect(final_stats.get("ok") is True, "final geometry query failed")
    _expect(
        isinstance(final_stats.get("geometry_stats"), Mapping),
        "final geometry stats are missing",
    )

    # The fixed gate semantics must also survive the real Bridge boundary.
    refused_id = f"bridge_orient_{token}"
    refused_coord = ScratchCoordinator(
        ScratchSessionContext(provider=provider, sandbox_id=refused_id)
    )
    refused_build = await refused_coord.build(
        operations=[{"kind": "create_node", "node_name": "box1", "node_type": "box"}]
    )
    _expect(refused_build.get("ok") is True, f"orientation fixture build failed: {refused_build}")
    refused = await refused_coord.commit(
        target_parent_path="/obj",
        target_name=f"eee_bridge_orientation_{token}",
        orientation_checks=[{"component_id": "box", "expected_axis": "Y"}],
        skip_structure_check=True,
    )
    _expect(
        refused.get("ok") is True and refused.get("refused") is True,
        f"orientation check did not refuse: {refused}",
    )
    gates = refused.get("gates")
    _expect(
        isinstance(gates, list)
        and any(g.get("gate") == "bake" and not g.get("passed") for g in gates),
        "Bridge refusal omitted the bake gate failure",
    )
    destroyed = await provider.scratch_destroy(sandbox_id=refused_id)
    _expect(destroyed.missing is False, "refused sandbox cleanup reported missing")

    return {
        "capability": "scratch.v1",
        "build_ok": True,
        "geometry_ok": True,
        "verify_geometry_ok": True,
        "commit_ok": True,
        "final_query_ok": True,
        "orientation_refusal_ok": True,
        "refused_sandbox_destroyed": True,
    }


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_EVIDENCE_BYTES:
        _die("evidence exceeds bounded size")
    path.write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hython", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not args.hython.is_file():
        _die("hython executable does not exist")

    process: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory(prefix="eee-scratch-bridge-") as raw:
        root = Path(raw)
        state_dir = root / "state"
        state_dir.mkdir()
        stop_file = state_dir / _STOP
        cleanup_file = state_dir / _CLEANUP
        worker_log = state_dir / "worker.log"
        hip_path = root / "scene.hip"
        token = f"{os.getpid()}_{int(time.time())}"
        try:
            process = _start_worker(
                hython=args.hython,
                state_dir=state_dir,
                hip_path=hip_path,
                stop_file=stop_file,
                cleanup_file=cleanup_file,
                worker_log=worker_log,
            )
            _wait_for_bridge(state_dir, process)
            evidence = asyncio.run(_journey(state_dir, token))
            cleanup = _stop_worker(
                process,
                stop_file=stop_file,
                cleanup_file=cleanup_file,
            )
            process = None
            evidence["worker_cleanup_ok"] = True
            evidence["worker_removed_count"] = cleanup["removed"]
            if args.evidence is not None:
                _write_evidence(args.evidence, evidence)
            print("SCRATCH BRIDGE JOURNEY OK")
            return 0
        finally:
            if process is not None:
                try:
                    _stop_worker(
                        process,
                        stop_file=stop_file,
                        cleanup_file=cleanup_file,
                    )
                except Exception:
                    process.kill()
                    _close_log(process)


if __name__ == "__main__":
    raise SystemExit(main())
