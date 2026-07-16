"""Test-only Runtime process fixture (spawned as an independent process).

Run as::

    python -m tests.runtime.runtime_process_fixture [complete|block|workspace|bridge|bridge_hang]

It opens the REAL ``RuntimeService`` and ``RuntimeWebSocketServer`` with a
deterministic fake ``RunnerFactory`` (no live LLM and no in-process HOM),
publishes the discovery/token files for the parent test to read, and blocks
until the parent terminates the process. ``bridge`` mode talks to an external
authenticated Houdini Bridge through the production provider. This deliberately
duplicates the production lifecycle (see ``eee_agent/runtime/__main__.py``)
instead of injecting a fake runner into
production code — there is no test-runner switch in the Runtime package.

Modes:

* ``complete`` (default): the fake runner yields a text delta, a model.completed
  event, and a terminal ``RunnerCompleted``. A started run reaches Completed.
* ``block``: the fake runner never yields a terminal event, so a started run
  stays non-terminal (Planning). Hard-killing the process leaves an interrupted
  run for restart-reconciliation coverage.
* ``workspace``: the completing runner plus a JSON-controlled trusted
  Workspace fact provider. Public tests still traverse the real Runtime
  parser/server/service/repository chain.
* ``bridge``: the completing runner plus the production
  ``BridgeWorkspaceFactProvider``. This mode is used by the real-Houdini smoke
  so Runtime stays in a standard Python process while HOM remains in hython.
  Creating ``runtime_fixture_stop`` in ``state_dir`` requests a graceful exit.
* ``bridge_hang``: the same production provider, but deliberately ignores the
  stop marker so parent-side launcher/worker process-tree cleanup can be tested.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from eee_agent.houdini_bridge.contracts import SceneBinding
from eee_agent.houdini_bridge.workspace_provider import (
    BridgeWorkspaceFactProvider,
)
from eee_agent.houdini_bridge.workspaces import (
    WorkspaceInspectResult,
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
    WorkspaceNodeObservation,
)
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
_WORKSPACE_CONTROL = "workspace_fixture.json"
_WORKSPACE_EVENT_ENTERED = "workspace_event_entered"
_RUNTIME_FIXTURE_STOP = "runtime_fixture_stop"


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


class _ControlledWorkspaceProvider:
    """Test-only provider controlled by a bounded JSON file in state_dir."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / _WORKSPACE_CONTROL

    def _load(self) -> dict:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise WorkspaceInspectionUnavailable("fixture offline") from exc
        if type(value) is not dict:
            raise WorkspaceInspectionConflict("fixture control must be an object")
        return value

    def fail_event_type(self) -> str | None:
        try:
            value = self._load().get("fail_event_type")
        except WorkspaceInspectionUnavailable:
            return None
        return value if type(value) is str else None

    def block_event_type(self) -> str | None:
        try:
            value = self._load().get("block_event_type")
        except WorkspaceInspectionUnavailable:
            return None
        return value if type(value) is str else None

    @property
    def event_entered_path(self) -> Path:
        return self._path.parent / _WORKSPACE_EVENT_ENTERED

    @staticmethod
    def _binding(instance_id: str, scene_epoch: int) -> SceneBinding:
        return SceneBinding(
            instance_id=instance_id,
            scene_epoch=scene_epoch,
            hip_path=None,
            observed_revision=f"fixture-scene-{scene_epoch}",
        )

    @staticmethod
    def _observations(values: object) -> tuple[WorkspaceNodeObservation, ...]:
        if type(values) is not list:
            raise WorkspaceInspectionConflict(
                "fixture observations must be a list"
            )
        try:
            return tuple(WorkspaceNodeObservation(**item) for item in values)
        except (TypeError, ValueError) as exc:
            raise WorkspaceInspectionConflict(
                "fixture observation is invalid"
            ) from exc

    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        config = self._load().get("selection")
        if type(config) is not dict:
            raise WorkspaceInspectionUnavailable("fixture selection unavailable")
        status = config.get("status")
        if status == "offline":
            raise WorkspaceInspectionUnavailable("fixture offline")
        if status == "conflict":
            raise WorkspaceInspectionConflict("fixture conflict")
        if status != "ok":
            raise WorkspaceInspectionConflict("fixture selection status invalid")
        instance_id = config.get("instance_id")
        scene_epoch = config.get("scene_epoch")
        if type(instance_id) is not str or type(scene_epoch) is not int:
            raise WorkspaceInspectionConflict("fixture binding invalid")
        if (
            expected_scene_epoch is not None
            and expected_scene_epoch != scene_epoch
        ):
            raise WorkspaceInspectionConflict("fixture stale scene")
        return WorkspaceInspectResult.build(
            binding=self._binding(instance_id, scene_epoch),
            mode="selection",
            observations=self._observations(config.get("observations")),
        )

    async def inspect_manifest(
        self, manifest, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult:
        status = self._load().get("manifest_status", "offline")
        if status == "offline":
            raise WorkspaceInspectionUnavailable("fixture offline")
        if status == "conflict":
            raise WorkspaceInspectionConflict("fixture conflict")
        if status not in ("healthy", "stale"):
            raise WorkspaceInspectionConflict("fixture manifest status invalid")
        observations = tuple(
            WorkspaceNodeObservation(
                path=(
                    f"{node.path}_stale"
                    if status == "stale"
                    else node.path
                ),
                node_type=node.node_type,
                parent_path=node.parent_path,
                is_locked=False,
                workspace_id=manifest.workspace_id,
                node_id=node.node_id,
                capability=node.capability,
                role=node.role,
                schema_version=manifest.schema_version,
                created_by_run=manifest.created_by_run,
            )
            for node in manifest.nodes
        )
        if (
            expected_scene_epoch is not None
            and expected_scene_epoch != manifest.scene_epoch
        ):
            raise WorkspaceInspectionConflict("fixture stale scene")
        return WorkspaceInspectResult.build(
            binding=self._binding(
                manifest.instance_id, manifest.scene_epoch
            ),
            mode="manifest",
            observations=observations,
        )


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
        controlled_workspace_provider = (
            _ControlledWorkspaceProvider(paths.state_dir)
            if mode == "workspace"
            else None
        )
        workspace_provider = (
            BridgeWorkspaceFactProvider(paths.state_dir)
            if mode in ("bridge", "bridge_hang")
            else controlled_workspace_provider
        )
        if controlled_workspace_provider is not None:
            try:
                controlled_workspace_provider.event_entered_path.unlink()
            except FileNotFoundError:
                pass
        stop_path = paths.state_dir / _RUNTIME_FIXTURE_STOP
        if mode in ("bridge", "bridge_hang"):
            try:
                stop_path.unlink()
            except FileNotFoundError:
                pass
        async with RuntimeService.open(
            paths,
            runner_factory=_runner_factory(mode),
            workspace_fact_provider=workspace_provider,
        ) as service:
            if controlled_workspace_provider is not None:
                original_append_conn = service._events._append_conn

                async def controlled_append_conn(conn, **kwargs):
                    event_type = kwargs.get("event_type")
                    if (
                        controlled_workspace_provider.block_event_type()
                        == event_type
                    ):
                        controlled_workspace_provider.event_entered_path.write_text(
                            str(event_type), encoding="utf-8"
                        )
                        while (
                            controlled_workspace_provider.block_event_type()
                            == event_type
                        ):
                            await asyncio.sleep(0.01)
                    if (
                        controlled_workspace_provider.fail_event_type()
                        == event_type
                    ):
                        raise RuntimeError("fixture event append failure")
                    return await original_append_conn(conn, **kwargs)

                service._events._append_conn = controlled_append_conn
            server = RuntimeWebSocketServer(
                service, identity, host=_BIND_HOST, port=0
            )
            async with server:
                if mode == "bridge_hang":
                    # Exceed a typical Windows pipe buffer before discovery.
                    # The parent must continuously drain stderr or this fixture
                    # will block here and never become ready.
                    for index in range(128):
                        print(
                            f"[fixture] stderr pressure {index}: "
                            + ("x" * 1024),
                            file=sys.stderr,
                            flush=True,
                        )
                # The server has bound its ephemeral port; publish discovery so
                # the parent can read it and authenticate.
                write_identity_files(
                    identity,
                    paths.state_dir,
                    host=_BIND_HOST,
                    port=server.port,
                )
                try:
                    # Ordinary process tests hard-kill the fixture to exercise
                    # restart reconciliation. The real-Houdini bridge mode uses
                    # a stop marker so identity/database cleanup is observable.
                    if mode == "bridge":
                        while not stop_path.exists():
                            await asyncio.sleep(0.05)
                    else:
                        await asyncio.Event().wait()
                finally:
                    cleanup_identity_files(identity, paths.state_dir)
                    if mode in ("bridge", "bridge_hang"):
                        try:
                            stop_path.unlink()
                        except FileNotFoundError:
                            pass


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "complete"
    if mode not in (
        "complete",
        "block",
        "workspace",
        "bridge",
        "bridge_hang",
    ):
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
