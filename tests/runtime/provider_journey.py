"""Real provider + real Houdini scratch-native Runtime journey adapter.

This is the adapter contract required by the acceptance harness
``tests/runtime/runtime_mvp_provider_e2e.py``: the harness invokes it via
``EEE_RUNTIME_MVP_PROVIDER_COMMAND`` with a disposable runtime home, HIP path,
and evidence path in the environment. It runs the full Runtime MVP journey
against a REAL Houdini Secure Bridge (hython subprocess running
``provider_journey_houdini_worker.py``) and a REAL LLM provider (credentials
from the inherited process environment only), then writes the strict bounded
evidence JSON record.

Journey: start hython bridge worker -> open RuntimeService with the exact
production wiring (BridgeWorkspaceFactProvider, BridgeChangeSetProvider,
build_agent_runner(saver, modeling=True), houdini_21_minimal_catalog) ->
create session -> run a scratch modeling brief through the real provider ->
require scratch_build -> verify_geometry -> scratch_commit tool evidence ->
read the committed path and geometry back through the Secure Bridge -> prove
the run sandbox disappeared -> close and reopen the service to prove durable
event replay (last_seq) -> scene cleanup in the Houdini worker -> write
strict bounded evidence.

Credential hygiene: provider output, prompts, and secrets are never printed
and never written into the evidence record. stdout carries only bounded
status lines; stderr carries only a bounded step/code pair on failure. The
hython worker gets a scrubbed environment (no API keys) and its output goes
to a log file inside the disposable runtime home, never to this process's
stdout. Any failure cleans up the hython process tree and exits non-zero.

The module intentionally imports only stdlib at module scope so the
environment/credential validation runs before ``eee_agent.config`` (which
loads ``.env``) can influence the process environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

_ENV_RUNTIME_HOME = "EEE_RUNTIME_HOME"
_ENV_HFS = "EEE_RUNTIME_MVP_HFS"
_ENV_HIP_PATH = "EEE_RUNTIME_MVP_HIP_PATH"
_ENV_EVIDENCE_PATH = "EEE_RUNTIME_MVP_EVIDENCE_PATH"

_CREDENTIAL_VARS = (
    "EEE_LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
)

_WORKER_SCRIPT = Path(__file__).with_name("provider_journey_houdini_worker.py")
_STOP_FILENAME = "provider_journey.stop"
_CLEANUP_FILENAME = "provider_journey_cleanup.json"
_WORKER_LOG_FILENAME = "provider_journey_hython.log"

_EVIDENCE_FIELDS = frozenset(
    {
        "run_status",
        "scratch_build_seen",
        "scratch_build_ok",
        "geometry_verified",
        "scratch_commit_seen",
        "commit_status",
        "commit_receipt_present",
        "final_path",
        "final_geometry_ok",
        "sandbox_absent",
        "restart_replay_last_seq",
        "scene_cleanup",
    }
)
_MAX_EVIDENCE_BYTES = 16 * 1024
_REPLAY_LIMIT = 1000
# Real provider runs emit thousands of delta events; pagination must remain
# bounded so a runaway event log cannot make the adapter unbounded.
_REPLAY_MAX_PAGES = 64
_BRIDGE_DISCOVERY_TIMEOUT_SECONDS = 180.0
_RUN_TIMEOUT_SECONDS = 600.0
_WORKER_STOP_TIMEOUT_SECONDS = 120.0
_TARGET_NAME = "eee_provider_scratch_table"
_FINAL_PATH = f"/obj/{_TARGET_NAME}"
_OUTPUT_NAME = "tabletop"
_REQUIRED_TOOLS = frozenset({"scratch_build", "verify_geometry", "scratch_commit"})

# Bounded modeling brief. It is persisted in runtime events (normal runtime
# behavior) but never copied into stdout/stderr or the evidence record.
_BRIEF = (
    "Use scratch_build with purpose='build the approved tabletop test asset' "
    "to create exactly one box node named tabletop in the run sandbox. Set "
    "sizex=2.0, sizey=0.25, and sizez=1.2 in the same build "
    "call. Inspect the cooked geometry returned by scratch_build. Then call "
    "verify_geometry on the returned output_node with min_verts=8, max_verts=8, "
    "min_faces=6, and max_faces=6. Only if verification succeeds, call "
    "scratch_commit with target_parent_path=/obj, target_name="
    f"{_TARGET_NAME}, skip_structure_check=true, and no orientation checks. "
    "Do not use proposals, changesets, approvals, or propose_modeling. Stop "
    "immediately after the successful commit."
)


def _tool_result(event: object) -> dict[str, object] | None:
    """Decode one complete, bounded tool result preview."""
    if getattr(event, "event_type", None) != "tool.completed":
        return None
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping) or payload.get("truncated") is not False:
        return None
    content = payload.get("content")
    if type(content) is not str:
        return None
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        return None
    return result if type(result) is dict else None


def _tool_events(events: list[object], name: str) -> tuple[list[object], list[object]]:
    started = [
        event
        for event in events
        if getattr(event, "event_type", None) == "tool.started"
        and isinstance(getattr(event, "payload", None), Mapping)
        and event.payload.get("name") == name
    ]
    completed = [
        event
        for event in events
        if getattr(event, "event_type", None) == "tool.completed"
        and isinstance(getattr(event, "payload", None), Mapping)
        and event.payload.get("name") == name
    ]
    return started, completed


def _commit_preview_is_success(event: object) -> bool:
    """Validate the stable prefix of the bounded scratch_commit preview.

    A commit result may exceed the 600-character event preview because it
    carries four gate details. The coordinator deliberately emits ``ok``,
    ``committed``, ``refused``, ``final_path``, and the compact ``receipt``
    before those variable-size fields, so those facts remain auditable even
    when truncated. The real-scene and sandbox checks below independently
    prove the promotion.
    """
    payload = getattr(event, "payload", None)
    if not isinstance(payload, Mapping):
        return False
    content = payload.get("content")
    if type(content) is not str:
        return False
    compact = "".join(content.split())
    expected = json.dumps(_FINAL_PATH)
    return (
        '"ok":true' in compact
        and '"committed":true' in compact
        and '"refused":false' in compact
        and f'"final_path":{expected}' in compact
        and '"receipt":{' in compact
    )


class _StepError(Exception):
    """Bounded journey failure; detail is always a short constant string."""

    __slots__ = ("step", "detail")

    def __init__(self, step: str, detail: str) -> None:
        self.step = step
        self.detail = detail
        super().__init__(f"{step}: {detail}")


@dataclass(frozen=True, slots=True)
class _JourneyConfig:
    paths: object  # RuntimePaths; typed as object to keep imports deferred
    hython: Path
    hip_path: Path
    evidence_path: Path
    stop_file: Path
    cleanup_file: Path
    worker_log: Path


def _status(message: str) -> None:
    """Emit one bounded status line (never provider content)."""
    print(f"provider-journey: {message}", flush=True)


def _load_config() -> _JourneyConfig:
    """Validate the harness-provided environment; raise _StepError on gaps."""
    for name in (_ENV_RUNTIME_HOME, _ENV_HFS, _ENV_HIP_PATH, _ENV_EVIDENCE_PATH):
        if not (os.getenv(name) or "").strip():
            raise _StepError("environment", f"{name} is required")

    # Credential check runs BEFORE any eee_agent import so a .env file cannot
    # mask a genuinely credential-less environment.
    if not any((os.getenv(name) or "").strip() for name in _CREDENTIAL_VARS):
        raise _StepError("environment", "provider credentials are unavailable")

    hython = Path(os.environ[_ENV_HFS])
    if not hython.is_file():
        raise _StepError("environment", "hython executable is unavailable")

    evidence_path = Path(os.environ[_ENV_EVIDENCE_PATH])
    if not evidence_path.parent.is_dir():
        raise _StepError("environment", "evidence directory is unavailable")

    from eee_agent.runtime.paths import RuntimePaths

    try:
        paths = RuntimePaths.from_environment()
    except ValueError:
        raise _StepError("environment", "EEE_RUNTIME_HOME is invalid") from None
    paths.create_used_directories()
    return _JourneyConfig(
        paths=paths,
        hython=hython,
        hip_path=Path(os.environ[_ENV_HIP_PATH]),
        evidence_path=evidence_path,
        stop_file=paths.state_dir / _STOP_FILENAME,
        cleanup_file=paths.state_dir / _CLEANUP_FILENAME,
        worker_log=paths.state_dir / _WORKER_LOG_FILENAME,
    )


def _scrubbed_worker_env() -> dict[str, str]:
    """Worker environment without credentials or LLM configuration."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.endswith("_API_KEY") or upper.startswith("EEE_LLM_"):
            continue
        if any(tag in upper for tag in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")):
            continue
        env[key] = value
    return env


def _start_worker(config: _JourneyConfig) -> subprocess.Popen:
    log = config.worker_log.open("w", encoding="utf-8", errors="replace")
    kwargs: dict[str, object] = {
        "cwd": str(Path(__file__).resolve().parents[2]),
        "env": _scrubbed_worker_env(),
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": subprocess.STDOUT,
        "text": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [
                str(config.hython),
                str(_WORKER_SCRIPT),
                "--state-dir",
                str(config.paths.state_dir),
                "--hip-path",
                str(config.hip_path),
                "--stop-file",
                str(config.stop_file),
                "--cleanup-file",
                str(config.cleanup_file),
            ],
            **kwargs,
        )
    except OSError:
        log.close()
        raise _StepError("worker_start", "hython process could not be started") from None
    # Stash the log handle so the log never reaches this process's stdout.
    process._eee_log = log  # type: ignore[attr-defined]
    return process


def _close_worker_log(process: subprocess.Popen) -> None:
    log = getattr(process, "_eee_log", None)
    if log is not None:
        try:
            log.close()
        except OSError:
            pass
        process._eee_log = None  # type: ignore[attr-defined]


def _kill_worker(process: subprocess.Popen) -> None:
    """Best-effort bounded teardown of the hython worker tree."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=15,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
    except OSError:
        pass
    finally:
        _close_worker_log(process)


def _wait_for_bridge(config: _JourneyConfig, process: subprocess.Popen) -> None:
    from eee_agent.houdini_bridge.auth import (
        BRIDGE_DISCOVERY_FILENAME,
        BRIDGE_TOKEN_FILENAME,
    )

    deadline = time.monotonic() + _BRIDGE_DISCOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise _StepError(
                "worker_start", f"hython worker exited early (code {return_code})"
            )
        token = config.paths.state_dir / BRIDGE_TOKEN_FILENAME
        discovery = config.paths.state_dir / BRIDGE_DISCOVERY_FILENAME
        if token.is_file() and discovery.is_file():
            return
        time.sleep(0.25)
    raise _StepError("worker_start", "bridge discovery was not published in time")


def _service_wiring(paths: object) -> dict[str, object]:
    """Production-identical RuntimeService wiring (mirrors runtime.__main__)."""
    from eee_agent.houdini_bridge.changeset_provider import BridgeChangeSetProvider
    from eee_agent.houdini_bridge.read_only_provider import BridgeReadOnlyProvider
    from eee_agent.houdini_bridge.workspace_provider import (
        BridgeWorkspaceFactProvider,
    )
    from eee_agent.modeling.catalog import houdini_21_minimal_catalog
    from eee_agent.runtime.agent_runner import build_agent_runner
    from eee_agent.runtime.__main__ import _vision_settings

    vision_provider, vision_timeout_seconds = _vision_settings()

    return {
        "runner_factory": lambda saver: build_agent_runner(saver, modeling=True),
        "changeset_bridge_provider": BridgeChangeSetProvider(paths.state_dir),
        "workspace_fact_provider": BridgeWorkspaceFactProvider(paths.state_dir),
        "modeling_catalog_provider": houdini_21_minimal_catalog,
        "read_only_provider": BridgeReadOnlyProvider(paths.state_dir),
        "vision_provider": vision_provider,
        "vision_timeout_seconds": vision_timeout_seconds,
    }


async def _run_journey(paths: object) -> tuple[dict[str, object], str]:
    """Run the scratch_build->verify_geometry->scratch_commit journey.

    Returns the partial evidence record (everything except ``scene_cleanup``)
    and the session id used for the restart replay.
    """
    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.models import RunStatus
    from eee_agent.runtime.service import RuntimeService

    with RuntimeLock(paths.lock_file):
        async with RuntimeService.open(paths, **_service_wiring(paths)) as service:
            session = await service.create_session("Runtime MVP provider journey")
            run = await service.start_run(session.session_id, _BRIEF)
            async with asyncio.timeout(_RUN_TIMEOUT_SECONDS):
                final = await service.wait_for_run(run.run_id)
            if final.status is not RunStatus.COMPLETED:
                raise _StepError("run", f"run finished as {final.status.value}")
            _status("provider run completed")

            events, _ = await _replay_all(service, session.session_id)
            tool_events = {
                name: _tool_events(events, name) for name in _REQUIRED_TOOLS
            }
            if any(not started or not completed for started, completed in tool_events.values()):
                raise _StepError(
                    "tools", "required scratch tool events are incomplete"
                )

            build_results = [
                result
                for event in tool_events["scratch_build"][1]
                if (result := _tool_result(event)) is not None
            ]
            build = next(
                (
                    result
                    for result in build_results
                    if result.get("ok") is True
                    and isinstance(result.get("geometry"), Mapping)
                    and result["geometry"].get("point_count") == 8
                    and result["geometry"].get("prim_count") == 6
                    and type(result.get("output_node")) is str
                    and result["output_node"].endswith(f"/{_OUTPUT_NAME}")
                ),
                None,
            )
            if build is None:
                raise _StepError("scratch_build", "no valid cooked build result")

            verify_results = [
                result
                for event in tool_events["verify_geometry"][1]
                if (result := _tool_result(event)) is not None
            ]
            verified = next(
                (
                    result
                    for result in verify_results
                    if result.get("ok") is True
                    and result.get("issues") == []
                    and isinstance(result.get("stats"), Mapping)
                    and result["stats"].get("verts") == 8
                    and result["stats"].get("faces") == 6
                ),
                None,
            )
            if verified is None:
                raise _StepError("verify_geometry", "geometry verification did not pass")

            commit_event = next(
                (
                    event
                    for event in tool_events["scratch_commit"][1]
                    if _commit_preview_is_success(event)
                ),
                None,
            )
            if commit_event is None:
                raise _StepError("scratch_commit", "no committed scratch result")

            from eee_agent.houdini_bridge.read_only_provider import (
                BridgeReadOnlyProvider,
            )
            from eee_agent.runtime.service import _sandbox_id_from_run

            read_only = BridgeReadOnlyProvider(paths.state_dir)
            final_query = await read_only.query_scene([_FINAL_PATH])
            if (
                final_query.get("ok") is not True
                or final_query.get("node_count") != 1
            ):
                raise _StepError("final_query", "committed path is unavailable")
            final_stats = await read_only.geometry_stats(
                f"{_FINAL_PATH}/{_OUTPUT_NAME}"
            )
            geometry = final_stats.get("geometry_stats")
            if (
                final_stats.get("ok") is not True
                or not isinstance(geometry, Mapping)
                or geometry.get("points") != 8
                or geometry.get("primitives") != 6
            ):
                raise _StepError("final_query", "committed geometry is invalid")
            sandbox_path = f"/obj/eee_scratch_{_sandbox_id_from_run(run.run_id)}"
            sandbox_query = await read_only.query_scene([sandbox_path])
            if (
                sandbox_query.get("ok") is not True
                or sandbox_query.get("node_count") != 0
            ):
                raise _StepError("sandbox_cleanup", "run sandbox still exists")
            _status("scratch tools and committed scene verified")

    evidence = {
        "run_status": "completed",
        "scratch_build_seen": True,
        "scratch_build_ok": True,
        "geometry_verified": True,
        "scratch_commit_seen": True,
        "commit_status": "committed",
        "commit_receipt_present": True,
        "final_path": _FINAL_PATH,
        "final_geometry_ok": True,
        "sandbox_absent": True,
    }
    return evidence, session.session_id


async def _replay_all(service: object, session_id: str) -> tuple[list[object], int]:
    """Page through the full durable event log for ``session_id``.

    A real provider run emits thousands of delta events, far beyond one page;
    the checks below need the approval/validation/artifact events near the end
    of the log. Pagination stays bounded by ``_REPLAY_MAX_PAGES``.
    """
    events: list[object] = []
    after_seq = 0
    last_seq = 0
    for _ in range(_REPLAY_MAX_PAGES):
        replay = await service.replay(session_id, after_seq=after_seq, limit=_REPLAY_LIMIT)
        page = list(replay.events)
        if type(replay.last_seq) is int:
            last_seq = max(last_seq, replay.last_seq)
        if not page:
            break
        events.extend(page)
        after_seq = page[-1].seq
        if len(page) < _REPLAY_LIMIT:
            break
    return events, last_seq


async def _restart_replay(paths: object, session_id: str) -> int:
    """Reopen the service and prove the durable event log replays."""
    from eee_agent.runtime.lock import RuntimeLock
    from eee_agent.runtime.service import RuntimeService

    with RuntimeLock(paths.lock_file):
        async with RuntimeService.open(paths, **_service_wiring(paths)) as service:
            events, last_seq = await _replay_all(service, session_id)
            if type(last_seq) is not int or last_seq < 1:
                raise _StepError("replay", "replay last_seq is invalid")
            for name in _REQUIRED_TOOLS:
                started, completed = _tool_events(events, name)
                if not started or not completed:
                    raise _StepError(
                        "replay", "scratch tool events did not survive restart"
                    )
    _status("restart replay confirmed")
    return last_seq


def _finish_scene_cleanup(
    config: _JourneyConfig, process: subprocess.Popen
) -> str:
    """Signal the worker, require a clean exit and a valid cleanup receipt."""
    config.stop_file.write_text("stop\n", encoding="utf-8")
    try:
        return_code = process.wait(timeout=_WORKER_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise _StepError("scene_cleanup", "hython worker did not stop in time") from None
    if return_code != 0:
        raise _StepError(
            "scene_cleanup", f"hython worker exited with code {return_code}"
        )
    try:
        receipt = json.loads(config.cleanup_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _StepError("scene_cleanup", "cleanup receipt is missing") from None
    if type(receipt) is not dict or type(receipt.get("removed")) is not int:
        raise _StepError("scene_cleanup", "cleanup receipt is invalid")
    _status("scene cleanup completed")
    return "completed"


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    if frozenset(evidence) != _EVIDENCE_FIELDS:
        raise _StepError("evidence", "evidence fields are invalid")
    payload = json.dumps(evidence, sort_keys=True).encode("utf-8")
    if len(payload) > _MAX_EVIDENCE_BYTES:
        raise _StepError("evidence", "evidence record is too large")
    path.write_bytes(payload)


def main() -> int:
    try:
        config = _load_config()
    except _StepError as exc:
        print(
            f"provider-journey: failed step={exc.step} detail={exc.detail}",
            file=sys.stderr,
        )
        return 1

    worker: subprocess.Popen | None = None
    try:
        worker = _start_worker(config)
        _wait_for_bridge(config, worker)
        _status("secure bridge ready")
        evidence, session_id = asyncio.run(_run_journey(config.paths))
        evidence["restart_replay_last_seq"] = asyncio.run(
            _restart_replay(config.paths, session_id)
        )
        evidence["scene_cleanup"] = _finish_scene_cleanup(config, worker)
        _close_worker_log(worker)
        worker = None
        _write_evidence(config.evidence_path, evidence)
    except _StepError as exc:
        print(
            f"provider-journey: failed step={exc.step} detail={exc.detail}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - bounded, leak-free failure line
        from eee_agent.core import AgentException

        code = exc.error.code if isinstance(exc, AgentException) else None
        detail = code if code else f"unexpected {type(exc).__name__}"
        print(f"provider-journey: failed step=journey detail={detail}", file=sys.stderr)
        return 1
    finally:
        if worker is not None:
            _kill_worker(worker)
    _status("evidence written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
