"""Task 16-D/B2b disposable real-Houdini smoke (run with hython, NOT pytest).

Exercises one minimal create + set + connect transaction through the real
:class:`ChangeSetExecutor` against the live ``hou`` module, then a receipt
query and an idempotent replay (``AlreadyApplied``). It then starts the real
Secure Bridge and Runtime WebSocket boundary to exercise trusted Workspace
selection/manifest inspection plus public create/bind/switch/inspect with
before/after scene fingerprints. It creates only a private
``/obj/eee_task16d_smoke`` branch, destroys it on exit, and never saves, loads,
clears, or exports the user's HIP.

Run (detected hython)::

    "<Houdini>/bin/hython.exe" tests/runtime/changeset_houdini_smoke.py

Exit code 0 on success; non-zero with a message on any failure. The module is
imported by hython only (``hou`` is unavailable under pytest/the venv), so it
guards on ``hou`` and is excluded from offline collection by name/convention.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE_PACKAGES = _REPO_ROOT / ".venv" / "Lib" / "site-packages"
_VENV_PYTHON = _REPO_ROOT / ".venv" / "Scripts" / "python.exe"
_RUNTIME_FIXTURE_MODULE = "tests.runtime.runtime_process_fixture"
_RUNTIME_FIXTURE_STOP = "runtime_fixture_stop"
for _path in (_REPO_ROOT, _VENV_SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


class _FingerprintReadError(RuntimeError):
    """A required Houdini no-write fingerprint surface was unreadable."""


def _required_read(surface: str, callable_):
    try:
        return callable_()
    except Exception as exc:
        raise _FingerprintReadError(
            "required Houdini fingerprint surface unreadable: "
            f"{surface} ({type(exc).__name__})"
        ) from exc


def _houdini_fingerprint(hou) -> str:
    """Read-only full-scene fingerprint for every B2b operation."""
    root = _required_read("hou.node('/')", lambda: hou.node("/"))
    if root is None:
        raise _FingerprintReadError(
            "required Houdini fingerprint surface unreadable: "
            "hou.node('/') returned None"
        )
    nodes = (
        root,
        *_required_read(
            "root.allSubChildren()",
            root.allSubChildren,
        ),
    )
    inventory = []
    for node in nodes:
        node_path = _required_read("node.path()", node.path)
        parms = []
        for parm in _required_read(
            f"node {node_path} parms",
            node.parms,
        ):
            parm_name = _required_read(
                f"node {node_path} parameter name",
                parm.name,
            )
            parms.append(
                (
                    parm_name,
                    _required_read(
                        f"node {node_path} parameter {parm_name} data",
                        lambda parm=parm: parm.asData(
                            value=True,
                            evaluate_value=False,
                            locked=True,
                            brief=False,
                            multiparm_instances=True,
                            metadata=True,
                            verbose=True,
                        ),
                    ),
                )
            )
        connections = []
        for connection in _required_read(
            f"node {node_path} input connections",
            node.inputConnections,
        ):
            input_node = _required_read(
                f"node {node_path} input connection source",
                connection.inputNode,
            )
            connections.append(
                (
                    _required_read(
                        f"node {node_path} input connection index",
                        connection.inputIndex,
                    ),
                    _required_read(
                        f"node {node_path} input connection output index",
                        connection.outputIndex,
                    ),
                    None
                    if input_node is None
                    else _required_read(
                        f"node {node_path} input connection source path",
                        input_node.path,
                    ),
                )
            )
        color = _required_read(f"node {node_path} color", node.color)
        parent = _required_read(f"node {node_path} parent", node.parent)
        flags = {}
        for flag_name, flag in (
            ("display", hou.nodeFlag.Display),
            ("render", hou.nodeFlag.Render),
            ("template", hou.nodeFlag.Template),
            ("bypass", hou.nodeFlag.Bypass),
            ("hard_locked", hou.nodeFlag.Lock),
            ("soft_locked", hou.nodeFlag.SoftLock),
        ):
            readable = _required_read(
                f"node {node_path} {flag_name} flag readability",
                lambda node=node, flag=flag: node.isFlagReadable(flag),
            )
            flags[flag_name] = {
                "readable": readable,
                "value": (
                    _required_read(
                        f"node {node_path} {flag_name} flag value",
                        lambda node=node, flag=flag: node.isGenericFlagSet(flag),
                    )
                    if readable
                    else None
                ),
            }
        inventory.append(
            {
                "path": node_path,
                "type": _required_read(
                    f"node {node_path} type",
                    lambda: node.type().name(),
                ),
                "parent": (
                    None
                    if parent is None
                    else _required_read(
                        f"node {node_path} parent path",
                        parent.path,
                    )
                ),
                "parms": parms,
                "inputs": connections,
                "user_data": sorted(
                    _required_read(
                        f"node {node_path} user data",
                        node.userDataDict,
                    ).items()
                ),
                "position": tuple(
                    _required_read(
                        f"node {node_path} position",
                        node.position,
                    )
                ),
                "color": tuple(
                    _required_read(
                        f"node {node_path} color RGB",
                        color.rgb,
                    )
                ),
                "comment": _required_read(
                    f"node {node_path} comment",
                    node.comment,
                ),
                "selected": _required_read(
                    f"node {node_path} selected flag",
                    node.isSelected,
                ),
                "flags": flags,
            }
        )
    undo_labels = _required_read(
        "hou.undos.undoLabels()",
        lambda: tuple(hou.undos.undoLabels()),
    )
    payload = {
        "hip_name": _required_read("hou.hipFile.name()", hou.hipFile.name),
        "hip_is_new": _required_read(
            "hou.hipFile.isNewFile()",
            hou.hipFile.isNewFile,
        ),
        "hip_has_unsaved_changes": _required_read(
            "hou.hipFile.hasUnsavedChanges()",
            hou.hipFile.hasUnsavedChanges,
        ),
        "undo_labels": undo_labels,
        "loaded_hdas": tuple(
            sorted(
                _required_read(
                    "hou.hda.loadedFiles()",
                    hou.hda.loadedFiles,
                )
            )
        ),
        "nodes": inventory,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _fingerprint_or_die(hou) -> str:
    try:
        return _houdini_fingerprint(hou)
    except _FingerprintReadError as exc:
        _die(str(exc))


def _select_exact(hou, nodes: tuple[object, ...]) -> None:
    hou.clearAllSelected()
    for node in nodes:
        node.setSelected(True, clear_all_selected=False)


async def _runtime_request(ws, request_id: str, command_type: str, payload: dict):
    from eee_agent.runtime.protocol import PROTOCOL, encode_envelope

    try:
        async with asyncio.timeout(15.0):
            await ws.send(
                encode_envelope(
                    {
                        "protocol": PROTOCOL,
                        "kind": "command",
                        "request_id": request_id,
                        "type": command_type,
                        "payload": payload,
                    }
                )
            )
            while True:
                response = json.loads(await ws.recv())
                if (
                    response.get("kind") == "response"
                    and response.get("request_id") == request_id
                ):
                    return response
    except TimeoutError:
        _die(
            "Runtime request timed out after 15 seconds: "
            f"{command_type} ({request_id})"
        )


class _RuntimeFixtureProcess:
    """Bounded launcher/worker lifecycle for the external Runtime smoke."""

    def __init__(
        self,
        home: Path,
        *,
        mode: str = "bridge",
        graceful_timeout: float = 15.0,
    ) -> None:
        self.home = home
        self.mode = mode
        self.graceful_timeout = graceful_timeout
        self.process: subprocess.Popen | None = None
        self.launcher_pid = 0
        self.worker_pid = 0
        self._stderr_lines: deque[str] = deque(maxlen=200)
        self._stderr_drainer: threading.Thread | None = None
        self._stopped = False
        self._cleanup_result: str | None = None

    def start(self) -> None:
        if not _VENV_PYTHON.is_file():
            _die(f"Runtime fixture Python is missing: {_VENV_PYTHON}")
        env = dict(os.environ)
        env["EEE_RUNTIME_HOME"] = str(self.home)
        kwargs: dict = {
            "cwd": str(_REPO_ROOT),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "text": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NO_WINDOW",
                0,
            )
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(
            [
                str(_VENV_PYTHON),
                "-m",
                _RUNTIME_FIXTURE_MODULE,
                self.mode,
            ],
            **kwargs,
        )
        self.launcher_pid = self.process.pid
        self._stderr_drainer = threading.Thread(
            target=self._drain_stderr,
            daemon=True,
        )
        self._stderr_drainer.start()

    def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            self._stderr_lines.append(line)

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr_lines)[-4000:]

    async def wait_for_discovery(self, paths) -> tuple[dict, str]:
        process = self.process
        if process is None:
            raise RuntimeError("Runtime fixture was not started")
        deadline = asyncio.get_running_loop().time() + 30.0
        while asyncio.get_running_loop().time() < deadline:
            return_code = process.poll()
            if return_code is not None:
                _die(
                    "Runtime fixture exited before discovery "
                    f"(code {return_code}): {self.stderr_tail}"
                )
            if paths.discovery_file.is_file() and paths.token_file.is_file():
                try:
                    discovery = json.loads(
                        paths.discovery_file.read_text(encoding="utf-8")
                    )
                    token = paths.token_file.read_text(encoding="utf-8")
                except (OSError, ValueError):
                    await asyncio.sleep(0.05)
                    continue
                worker_pid = discovery.get("pid") if type(discovery) is dict else None
                if (
                    type(discovery) is dict
                    and discovery.get("host") == "127.0.0.1"
                    and type(discovery.get("port")) is int
                    and type(worker_pid) is int
                    and worker_pid > 0
                    and token
                ):
                    self.worker_pid = worker_pid
                    return discovery, token
            await asyncio.sleep(0.05)
        _die("Runtime fixture did not publish discovery within 30 seconds")

    async def stop(self, paths) -> str | None:
        if self._stopped:
            return self._cleanup_result
        self._stopped = True
        process = self.process
        if process is None:
            return None

        stop_path = paths.state_dir / _RUNTIME_FIXTURE_STOP
        stop_path.write_text("stop\n", encoding="utf-8")
        deadline = (
            asyncio.get_running_loop().time() + self.graceful_timeout
        )
        while (
            self._tree_is_alive()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)

        forced = self._tree_is_alive()
        if forced:
            self._force_tree()
            force_deadline = asyncio.get_running_loop().time() + 10.0
            while (
                self._tree_is_alive()
                and asyncio.get_running_loop().time() < force_deadline
            ):
                await asyncio.sleep(0.05)

        if self._stderr_drainer is not None:
            self._stderr_drainer.join(timeout=2.0)
        process.poll()

        if forced:
            self._remove_runtime_handoff(paths)
        if self._tree_is_alive():
            self._cleanup_result = (
                "Runtime fixture process tree survived forced termination"
            )
        elif forced:
            self._cleanup_result = (
                "Runtime fixture required forced termination"
            )
        elif process.returncode not in (0, None):
            self._cleanup_result = (
                f"Runtime fixture exited with code {process.returncode}: "
                f"{self.stderr_tail}"
            )
        return self._cleanup_result

    def _tree_is_alive(self) -> bool:
        pids = {self.launcher_pid, self.worker_pid}
        return any(self.pid_exists(pid) for pid in pids if pid > 0)

    def _force_tree(self) -> None:
        pids = tuple(
            dict.fromkeys(
                pid
                for pid in (self.worker_pid, self.launcher_pid)
                if pid > 0
            )
        )
        if os.name == "nt":
            for pid in pids:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                        creationflags=getattr(
                            subprocess,
                            "CREATE_NO_WINDOW",
                            0,
                        ),
                    )
                except subprocess.TimeoutExpired:
                    pass
            return
        try:
            os.killpg(os.getpgid(self.launcher_pid), 9)
        except (ProcessLookupError, PermissionError):
            pass
        for pid in pids:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass

    @staticmethod
    def pid_exists(pid: int) -> bool:
        if type(pid) is not int or pid < 1:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True

        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    @staticmethod
    def _remove_runtime_handoff(paths) -> None:
        for path in (
            paths.token_file,
            paths.discovery_file,
            paths.state_dir / _RUNTIME_FIXTURE_STOP,
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


async def _assert_b2b_no_houdini_write(
    hou,
    adapter,
    label: str,
    operation,
):
    before = _fingerprint_or_die(hou)
    epoch_before = adapter.binding().scene_epoch
    result = await operation
    after = _fingerprint_or_die(hou)
    epoch_after = adapter.binding().scene_epoch
    if before != after:
        _die(f"{label} mutated the Houdini scene fingerprint")
    if epoch_before != epoch_after:
        _die(f"{label} changed scene epoch {epoch_before} -> {epoch_after}")
    print(f"b2b no-write: {label}")
    return result


async def _workspace_lifecycle_smoke(
    hou,
    adapter,
    *,
    root,
    child,
    alternate,
    ordinary,
    partial,
    workspace_id: str,
    second_workspace_id: str,
) -> None:
    from websockets.asyncio.client import connect

    from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest
    from eee_agent.changesets.repository import ChangeSetRepository
    from eee_agent.core import AgentException
    from eee_agent.houdini_bridge.auth import (
        BRIDGE_DISCOVERY_FILENAME,
        BRIDGE_TOKEN_FILENAME,
        create_bridge_identity,
    )
    from eee_agent.houdini_bridge.client import BridgeClient
    from eee_agent.houdini_bridge.queue import MainThreadReadQueue
    from eee_agent.houdini_bridge.workspaces import WorkspaceInspectRequest
    from eee_agent.runtime.database import RuntimeDatabase
    from eee_agent.runtime.paths import RuntimePaths
    from houdini_side.secure_bridge import BridgeServer

    async def pump(queue: MainThreadReadQueue, stop: asyncio.Event) -> None:
        while not stop.is_set():
            queue.pump_one()
            await asyncio.sleep(0.002)

    _fingerprint_or_die(hou)
    print("b2b fingerprint: all required Houdini read surfaces are available")

    old_runtime_home = os.environ.get("EEE_RUNTIME_HOME")
    with tempfile.TemporaryDirectory(prefix="eee-b2b-smoke-") as temp_home:
        os.environ["EEE_RUNTIME_HOME"] = str(Path(temp_home).resolve())
        paths = RuntimePaths.from_environment()
        paths.create_used_directories()
        bridge_queue = MainThreadReadQueue()
        bridge_identity = create_bridge_identity()
        adapter.install_scene_epoch_callbacks()
        bridge_server = BridgeServer(
            adapter=adapter,
            identity=bridge_identity,
            state_dir=paths.state_dir,
            queue=bridge_queue,
        )
        listener = await asyncio.start_server(
            bridge_server.handle_connection, "127.0.0.1", 0
        )
        await bridge_server.serve(listener, host="127.0.0.1")
        stop = asyncio.Event()
        pump_task = asyncio.create_task(pump(bridge_queue, stop))
        runtime_fixture = None
        database = None
        duplicate = None
        runtime_cleanup_error = None
        try:
            runtime_fixture = _RuntimeFixtureProcess(paths.home)
            runtime_fixture.start()
            runtime_discovery, runtime_token = (
                await runtime_fixture.wait_for_discovery(
                    paths,
                )
            )
            if runtime_token == bridge_identity.token:
                _die("Runtime and Bridge unexpectedly share one bearer token")
            database = await RuntimeDatabase.open(paths.app_db)
            async with connect(
                "ws://"
                f"{runtime_discovery['host']}:{runtime_discovery['port']}",
                additional_headers={
                    "Authorization": f"Bearer {runtime_token}"
                },
                compression=None,
            ) as ws:
                    session_response = await _runtime_request(
                        ws,
                        "b2b-session",
                        "session.create",
                        {"title": "Houdini B2b smoke"},
                    )
                    if not session_response["ok"]:
                        _die(f"Runtime session.create failed: {session_response}")
                    session_id = session_response["result"]["session_id"]
                    run_response = await _runtime_request(
                        ws,
                        "b2b-run",
                        "run.start",
                        {
                            "session_id": session_id,
                            "user_input": "real Houdini workspace fixture",
                        },
                    )
                    if not run_response["ok"]:
                        _die(f"Runtime run.start failed: {run_response}")
                    run_id = run_response["result"]["run_id"]
                    for index in range(200):
                        snapshot = await _runtime_request(
                            ws,
                            f"b2b-run-snapshot-{index}",
                            "session.snapshot",
                            {"session_id": session_id},
                        )
                        runs = {
                            item["run_id"]: item
                            for item in snapshot["result"]["runs"]
                        }
                        if runs[run_id]["status"] == "Completed":
                            break
                        await asyncio.sleep(0.01)
                    else:
                        _die("Runtime fixture run did not reach Completed")

                    for node in (root, child, alternate):
                        node.setUserData("eee.created_by_run", run_id)

                    _select_exact(hou, (root, child))
                    binding = adapter.binding()
                    selection_request = WorkspaceInspectRequest(
                        request_id="b2b-bridge-selection",
                        deadline_ms=5000,
                        scene_epoch=binding.scene_epoch,
                        mode="selection",
                        manifest=None,
                    )
                    bridge_client = BridgeClient.from_state_dir(paths.state_dir)
                    async with bridge_client:
                        selection = await _assert_b2b_no_houdini_write(
                            hou,
                            adapter,
                            "Bridge selection inspection",
                            bridge_client.inspect_workspace(selection_request),
                        )
                    if len(selection.observations) != 2:
                        _die(
                            "Bridge selection inspection did not return the exact "
                            "two selected owned nodes"
                        )
                    for observation in selection.observations:
                        mirrors = (
                            observation.workspace_id,
                            observation.node_id,
                            observation.capability,
                            observation.role,
                            observation.schema_version,
                            observation.created_by_run,
                        )
                        if any(value is None for value in mirrors):
                            _die(
                                "Bridge selection inspection omitted an EEE mirror"
                            )

                    create_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime workspace.create",
                        _runtime_request(
                            ws,
                            "b2b-create",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": binding.scene_epoch,
                            },
                        ),
                    )
                    if not create_response["ok"]:
                        _die(f"Runtime workspace.create failed: {create_response}")
                    if not create_response["result"]["changed"]:
                        _die("Runtime workspace.create did not create state")
                    created_revision = create_response["result"]["workspace"][
                        "revision"
                    ]

                    repository = ChangeSetRepository(database)
                    created_manifest = await repository.get_workspace(
                        workspace_id
                    )
                    _select_exact(hou, (ordinary,))
                    manifest_request = WorkspaceInspectRequest(
                        request_id="b2b-bridge-manifest",
                        deadline_ms=5000,
                        scene_epoch=adapter.binding().scene_epoch,
                        mode="manifest",
                        manifest=created_manifest,
                    )
                    bridge_client = BridgeClient.from_state_dir(paths.state_dir)
                    async with bridge_client:
                        manifest_result = await _assert_b2b_no_houdini_write(
                            hou,
                            adapter,
                            "Bridge manifest inspection",
                            bridge_client.inspect_workspace(manifest_request),
                        )
                    if {
                        item.node_id for item in manifest_result.observations
                    } != {"n_root", "n_child"}:
                        _die(
                            "Bridge manifest inspection did not ignore unrelated "
                            "selection"
                        )

                    root.setName(
                        "eee_task16d_smoke_bound", unique_name=False
                    )
                    _select_exact(hou, (root, child))
                    await asyncio.sleep(0)
                    rebound_binding = adapter.binding()
                    bind_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime workspace.bind",
                        _runtime_request(
                            ws,
                            "b2b-bind",
                            "workspace.bind",
                            {
                                "session_id": session_id,
                                "workspace_id": workspace_id,
                                "expected_manifest_revision": created_revision,
                                "expected_scene_epoch": rebound_binding.scene_epoch,
                            },
                        ),
                    )
                    if not bind_response["ok"]:
                        _die(f"Runtime workspace.bind failed: {bind_response}")
                    if not bind_response["result"]["changed"]:
                        _die("Runtime workspace.bind did not refresh renamed paths")
                    if bind_response["result"]["workspace"]["revision"] == (
                        created_revision
                    ):
                        _die("Runtime workspace.bind kept the stale revision")

                    second_binding = adapter.binding()
                    alternate_ref = OwnedNodeRef(
                        node_id="n_alternate",
                        path=alternate.path(),
                        node_type=alternate.type().name(),
                        parent_path=alternate.parent().path(),
                        capability="modeling",
                        role="root",
                    )
                    second_manifest = WorkspaceManifest.build(
                        workspace_id=second_workspace_id,
                        session_id=session_id,
                        instance_id=second_binding.instance_id,
                        scene_epoch=second_binding.scene_epoch,
                        roots=(alternate_ref,),
                        nodes=(alternate_ref,),
                        created_by_run=run_id,
                        updated_at=datetime.now(timezone.utc),
                    )
                    await repository.insert_workspace(second_manifest)
                    switch_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime workspace.switch",
                        _runtime_request(
                            ws,
                            "b2b-switch",
                            "workspace.switch",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_active_workspace_id": workspace_id,
                                "expected_scene_epoch": second_binding.scene_epoch,
                            },
                        ),
                    )
                    if not switch_response["ok"]:
                        _die(f"Runtime workspace.switch failed: {switch_response}")
                    if switch_response["result"]["active_workspace_id"] != (
                        second_workspace_id
                    ):
                        _die("Runtime workspace.switch did not persist active target")
                    switched_state_revision = switch_response["result"][
                        "state_revision"
                    ]
                    switch_boundary_response = await _runtime_request(
                        ws,
                        "b2b-switch-boundary",
                        "events.replay",
                        {
                            "session_id": session_id,
                            "after_seq": 0,
                            "limit": 1000,
                        },
                    )
                    if not switch_boundary_response["ok"]:
                        _die(
                            "Runtime events.replay failed after switch: "
                            f"{switch_boundary_response}"
                        )
                    switch_boundary = switch_boundary_response["result"][
                        "last_seq"
                    ]

                    inspect_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime workspace.inspect",
                        _runtime_request(
                            ws,
                            "b2b-inspect",
                            "workspace.inspect",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_scene_epoch": second_binding.scene_epoch,
                            },
                        ),
                    )
                    if (
                        not inspect_response["ok"]
                        or inspect_response["result"]["status"] != "Healthy"
                    ):
                        _die(f"Runtime workspace.inspect failed: {inspect_response}")

                    alternate.setName(
                        "alternate_workspace_stale",
                        unique_name=False,
                    )
                    await asyncio.sleep(0)
                    stale_manifest_binding = adapter.binding()
                    stale_manifest_inspect = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime stale manifest inspection",
                        _runtime_request(
                            ws,
                            "b2b-stale-manifest-inspect",
                            "workspace.inspect",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_scene_epoch": (
                                    stale_manifest_binding.scene_epoch
                                ),
                            },
                        ),
                    )
                    if (
                        not stale_manifest_inspect["ok"]
                        or stale_manifest_inspect["result"]["status"] != "Stale"
                    ):
                        _die(
                            "renamed live node did not make the persisted "
                            f"manifest stale: {stale_manifest_inspect}"
                        )
                    if (
                        stale_manifest_inspect["result"][
                            "active_workspace_id"
                        ]
                        != second_workspace_id
                    ):
                        _die(
                            "stale manifest inspection changed the active "
                            "workspace state"
                        )

                    stale_manifest_switch = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime stale manifest switch rejection",
                        _runtime_request(
                            ws,
                            "b2b-stale-manifest-switch",
                            "workspace.switch",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_active_workspace_id": (
                                    second_workspace_id
                                ),
                                "expected_scene_epoch": (
                                    stale_manifest_binding.scene_epoch
                                ),
                            },
                        ),
                    )
                    if (
                        stale_manifest_switch["ok"]
                        or stale_manifest_switch["error"]["code"]
                        != "workspace.revision_conflict"
                    ):
                        _die(
                            "stale persisted manifest did not fail closed: "
                            f"{stale_manifest_switch}"
                        )

                    post_stale_switch = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime state after stale manifest rejection",
                        _runtime_request(
                            ws,
                            "b2b-post-stale-manifest",
                            "workspace.inspect",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_scene_epoch": (
                                    stale_manifest_binding.scene_epoch
                                ),
                            },
                        ),
                    )
                    if (
                        not post_stale_switch["ok"]
                        or post_stale_switch["result"]["status"] != "Stale"
                        or post_stale_switch["result"][
                            "active_workspace_id"
                        ]
                        != second_workspace_id
                    ):
                        _die(
                            "stale manifest switch changed the prior active "
                            f"pointer: {post_stale_switch}"
                        )

                    alternate.setName(
                        "alternate_workspace",
                        unique_name=False,
                    )
                    await asyncio.sleep(0)
                    restored_binding = adapter.binding()
                    preserved_noop = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime state after restored manifest",
                        _runtime_request(
                            ws,
                            "b2b-restored-manifest-noop",
                            "workspace.switch",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_active_workspace_id": (
                                    second_workspace_id
                                ),
                                "expected_scene_epoch": (
                                    restored_binding.scene_epoch
                                ),
                            },
                        ),
                    )
                    if (
                        not preserved_noop["ok"]
                        or preserved_noop["result"]["changed"]
                        or preserved_noop["result"]["active_workspace_id"]
                        != second_workspace_id
                        or preserved_noop["result"]["state_revision"]
                        != switched_state_revision
                    ):
                        _die(
                            "stale manifest rejection changed the prior active "
                            f"pointer or revision: {preserved_noop}"
                        )
                    after_stale_manifest = await _runtime_request(
                        ws,
                        "b2b-after-stale-manifest",
                        "events.replay",
                        {
                            "session_id": session_id,
                            "after_seq": switch_boundary,
                            "limit": 100,
                        },
                    )
                    if (
                        not after_stale_manifest["ok"]
                        or after_stale_manifest["result"]["events"]
                    ):
                        _die(
                            "stale inspect/switch or restored no-op emitted an "
                            f"unexpected event: {after_stale_manifest}"
                        )

                    _select_exact(hou, (ordinary,))
                    ordinary_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime ordinary selection rejection",
                        _runtime_request(
                            ws,
                            "b2b-ordinary",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": adapter.binding().scene_epoch,
                            },
                        ),
                    )
                    if (
                        ordinary_response["ok"]
                        or ordinary_response["error"]["code"]
                        != "workspace.unowned_selection"
                    ):
                        _die(
                            "ordinary unmarked selection did not fail closed: "
                            f"{ordinary_response}"
                        )

                    _select_exact(hou, (partial,))
                    partial_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime partial mirror rejection",
                        _runtime_request(
                            ws,
                            "b2b-partial",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": adapter.binding().scene_epoch,
                            },
                        ),
                    )
                    if (
                        partial_response["ok"]
                        or partial_response["error"]["code"]
                        != "workspace.incomplete_selection"
                    ):
                        _die(
                            "partial mirrors did not fail closed: "
                            f"{partial_response}"
                        )

                    duplicate = root.createNode("geo", "duplicate_identity")
                    for key, value in {
                        "eee.workspace_id": workspace_id,
                        "eee.node_id": "n_root",
                        "eee.capability": "modeling",
                        "eee.role": "root",
                        "eee.schema_version": "1",
                        "eee.created_by_run": run_id,
                    }.items():
                        duplicate.setUserData(key, value)
                    _select_exact(hou, (root,))
                    duplicate_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime duplicate identity rejection",
                        _runtime_request(
                            ws,
                            "b2b-duplicate",
                            "workspace.create",
                            {
                                "session_id": session_id,
                                "expected_scene_epoch": adapter.binding().scene_epoch,
                            },
                        ),
                    )
                    if (
                        duplicate_response["ok"]
                        or duplicate_response["error"]["code"]
                        != "workspace.identity_conflict"
                    ):
                        _die(
                            "duplicate stable identity did not fail closed: "
                            f"{duplicate_response}"
                        )
                    duplicate.destroy()
                    duplicate = None
                    await asyncio.sleep(0)

                    stale_response = await _assert_b2b_no_houdini_write(
                        hou,
                        adapter,
                        "Runtime stale binding rejection",
                        _runtime_request(
                            ws,
                            "b2b-stale",
                            "workspace.inspect",
                            {
                                "session_id": session_id,
                                "workspace_id": second_workspace_id,
                                "expected_scene_epoch": (
                                    adapter.binding().scene_epoch + 1
                                ),
                            },
                        ),
                    )
                    if (
                        stale_response["ok"]
                        or stale_response["error"]["code"]
                        != "bridge.stale_scene"
                    ):
                        _die(
                            "stale scene epoch was not preserved as a structured "
                            f"error: {stale_response}"
                        )

                    print(
                        "B2B SMOKE OK: Bridge selection/manifest inspection and "
                        "public Runtime create/bind/switch/inspect are read-only"
                    )
                    # Houdini 21.0.440's haio transport has a broken graceful
                    # EOF path (it references ``_sock`` instead of ``_socket``),
                    # which prevents the server connection from detaching and
                    # makes websockets.Server.wait_closed() hang forever.
                    # All public responses are complete here; abort only this
                    # disposable smoke client to exercise the working
                    # connection_lost path. The Runtime server itself runs in a
                    # standard Python subprocess and isn't exposed to haio.
                    ws.transport.abort()
                    await ws.wait_closed()
        except AgentException as exc:
            _die(
                f"structured B2b setup failure {exc.error.code}: "
                f"{exc.error.message_for_user}"
            )
        finally:
            if duplicate is not None:
                duplicate.destroy()
            if database is not None:
                await database.close()
            runtime_cleanup_error = (
                None
                if runtime_fixture is None
                else await runtime_fixture.stop(paths)
            )
            leftover_runtime_identity = [
                path.name
                for path in (paths.token_file, paths.discovery_file)
                if path.exists()
            ]
            stop.set()
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass
            await bridge_server.stop()
            leftover_identity = [
                filename
                for filename in (
                    BRIDGE_TOKEN_FILENAME,
                    BRIDGE_DISCOVERY_FILENAME,
                )
                if (paths.state_dir / filename).exists()
            ]
            if old_runtime_home is None:
                os.environ.pop("EEE_RUNTIME_HOME", None)
            else:
                os.environ["EEE_RUNTIME_HOME"] = old_runtime_home
            if runtime_cleanup_error is not None:
                _die(runtime_cleanup_error)
            if leftover_runtime_identity:
                _die(
                    "Runtime shutdown left identity files: "
                    + ", ".join(leftover_runtime_identity)
                )
            if leftover_identity:
                _die(
                    "Bridge shutdown left identity files: "
                    + ", ".join(leftover_identity)
                )


def main() -> None:
    try:
        import hou  # noqa: F401  — only available inside hython
    except ImportError:
        _die("hou is not importable; run this script with hython, not pytest/python.")

    from eee_agent.changesets import (
        ChangeSet,
        CheckpointPlan,
        ConnectInput,
        CreateNode,
        NodeRef,
        OwnedNodeRef,
        PermissionMode,
        RiskSummary,
        SetParm,
        WorkspaceManifest,
    )
    from eee_agent.houdini_bridge.changesets import ApplyRequest
    from houdini_side.changeset_executor import ChangeSetExecutor
    from houdini_side.secure_bridge import create_houdini_scene_adapter

    SES = f"ses_{'0' * 32}"
    RUN = f"run_{'1' * 32}"
    WS = f"ws_{'a' * 32}"
    WS2 = f"ws_{'b' * 32}"
    ROOT_PATH = "/obj/eee_task16d_smoke"
    CHILD_PATH = ROOT_PATH + "/geo1"  # geometry object (SOP container)
    SUBNET_PATH = CHILD_PATH + "/d1subnet"  # D1: created SOP subnet (parent)
    BOX_PATH = SUBNET_PATH + "/boxsrc"  # D1: created box SOP inside the subnet
    XFORM_PATH = SUBNET_PATH + "/xformtgt"  # D1: created xform SOP inside the subnet
    NOW = datetime.now(timezone.utc)

    def mirror(
        node, node_id: str, role: str, *, workspace_id: str = WS
    ) -> None:
        node.setUserData("eee.workspace_id", workspace_id)
        node.setUserData("eee.node_id", node_id)
        node.setUserData("eee.capability", "modeling")
        node.setUserData("eee.role", role)
        node.setUserData("eee.schema_version", "1")
        node.setUserData("eee.created_by_run", RUN)

    adapter = create_houdini_scene_adapter()
    binding = adapter.binding()

    # Fresh disposable namespace (destroy any prior smoke branch, then rebuild).
    pre = hou.node(ROOT_PATH)
    if pre is not None:
        pre.destroy()
    root = None
    try:
        root = hou.node("/obj").createNode("subnet", "eee_task16d_smoke")
        child = root.createNode("geo", "geo1")  # empty SOP container
        mirror(root, "n_root", "root")
        mirror(child, "n_child", "member")

        owned_root = OwnedNodeRef(
            node_id="n_root", path=ROOT_PATH, node_type="subnet",
            parent_path="/obj", capability="modeling", role="root",
        )
        owned_child = OwnedNodeRef(
            node_id="n_child", path=CHILD_PATH, node_type="geo",
            parent_path=ROOT_PATH, capability="modeling", role="member",
        )
        manifest = WorkspaceManifest.build(
            workspace_id=WS, session_id=SES, instance_id=binding.instance_id,
            scene_epoch=binding.scene_epoch, roots=[owned_root],
            nodes=[owned_root, owned_child],
            created_by_run=RUN, updated_at=NOW,
        )

        child_ref = NodeRef(
            node_id="n_child", path=CHILD_PATH, expected_type="geo", expected_workspace_id=WS,
        )
        subnet_ref = NodeRef(
            node_id="n_subnet", path=SUBNET_PATH, expected_type="subnet", expected_workspace_id=WS,
        )
        box_ref = NodeRef(
            node_id="n_box", path=BOX_PATH, expected_type="box", expected_workspace_id=WS,
        )
        xform_ref = NodeRef(
            node_id="n_xform", path=XFORM_PATH, expected_type="xform", expected_workspace_id=WS,
        )
        # D1: create SOP subnet, create box and xform inside it, set + connect.
        create_subnet = CreateNode(
            op_id="op_c1", parent=child_ref, node_id="n_subnet", node_type="subnet", node_name="d1subnet",
            workspace_id=WS, capability="modeling", role="member",
        )
        create_box = CreateNode(
            op_id="op_c2", parent=subnet_ref, node_id="n_box", node_type="box", node_name="boxsrc",
            workspace_id=WS, capability="modeling", role="member",
        )
        create_xform = CreateNode(
            op_id="op_c3", parent=subnet_ref, node_id="n_xform", node_type="xform", node_name="xformtgt",
            workspace_id=WS, capability="modeling", role="member",
        )
        setparm = SetParm(
            op_id="op_s", target=xform_ref, parm_name="tx", value=7.0, expected_old_value=0.0,
        )
        connect = ConnectInput(
            op_id="op_w", target=xform_ref, input_index=0, source=box_ref,
            source_output_index=0, expected_old_source=None,
        )
        ops = (create_subnet, create_box, create_xform, setparm, connect)
        # Derive every mandatory pre/postcondition + checkpoint fact so the
        # executor's F2 enforcement accepts the transaction.
        from houdini_side.changeset_executor import (  # noqa: E402
            _dedup,
            derive_mandatory_checkpoint,
            derive_mandatory_postconditions,
            derive_mandatory_preconditions,
        )
        pre = tuple(_dedup(derive_mandatory_preconditions(ops, binding, manifest)))
        post = tuple(_dedup(derive_mandatory_postconditions(ops)))
        cnodes, cparms, cwires = derive_mandatory_checkpoint(ops)
        changeset = ChangeSet(
            change_id=f"chg_{'5' * 32}", session_id=SES, run_id=RUN, scene_binding=binding,
            workspace_id=WS, base_revision="a" * 64,
            required_permission=PermissionMode.OWNED_WORKSPACE, scoped_node_ids=(),
            operations=ops,
            affected_nodes=(subnet_ref, box_ref, xform_ref), read_dependencies=(),
            preconditions=pre, expected_postconditions=post,
            risk_summary=RiskSummary(
                touches_external_nodes=False, changes_wiring=True, requires_backup=False,
                operation_count=5, effect_names=("node.create", "parm.set", "wire.connect"),
                affected_paths=(SUBNET_PATH, BOX_PATH, XFORM_PATH),
            ),
            checkpoint_plan=CheckpointPlan(
                nodes=tuple(_dedup(cnodes)), parameters=tuple(_dedup(cparms)), wires=tuple(_dedup(cwires)),
            ),
            created_at=NOW,
        )
        request = ApplyRequest.build(
            request_id="req_smoke", deadline_ms=30000, scene_epoch=binding.scene_epoch,
            changeset=changeset, workspace=manifest,
        )

        executor = ChangeSetExecutor(adapter)
        receipt = executor.apply(request)
        print(f"apply status={receipt.status.value} ops={receipt.applied_op_ids} "
              f"scene_may_have_changed={receipt.scene_may_have_changed}")
        if receipt.status.value != "Applied":
            _die(
                f"expected Applied, got {receipt.status.value}; "
                f"post={receipt.postcondition_results}; rollback={receipt.rollback_results}"
            )
        if receipt.applied_op_ids != ("op_c1", "op_c2", "op_c3", "op_s", "op_w"):
            _die(f"unexpected applied op order: {receipt.applied_op_ids}")

        # verify the real scene reflects the D1 writes
        if hou.node(SUBNET_PATH) is None:
            _die("D1 created subnet SOP not present after apply")
        if hou.node(BOX_PATH) is None:
            _die("D1 created box SOP not present after apply")
        if hou.node(XFORM_PATH) is None:
            _die("D1 created xform SOP not present after apply")
        xform = hou.node(XFORM_PATH)
        if xform.parm("tx").eval() != 7.0:
            _die("D1 created xform parm tx not set to 7.0 after apply")
        wired = xform.inputs()[0] if xform.inputs() else None
        if wired is None or wired.path() != BOX_PATH:
            _die("D1 xform input 0 not wired to the created box SOP after apply")

        # receipt query returns the cached terminal receipt (no scene mutation)
        cached = executor.receipt(changeset.change_id, changeset.digest)
        if cached is None or cached.status.value != "Applied":
            _die("receipt query did not return the cached Applied receipt")

        # idempotent replay performs zero writes and returns AlreadyApplied
        replay = executor.apply(request)
        print(f"replay status={replay.status.value} ops={replay.applied_op_ids}")
        if replay.status.value != "AlreadyApplied":
            _die(f"expected AlreadyApplied on replay, got {replay.status.value}")

        # B2b fixture setup. These writes happen outside every measured B2b
        # operation. The Workspace inspector must never infer descendants.
        alternate = root.createNode("geo", "alternate_workspace")
        mirror(
            alternate,
            "n_alternate",
            "root",
            workspace_id=WS2,
        )
        ordinary = root.createNode("geo", "ordinary_unmarked")
        partial = root.createNode("geo", "partial_identity")
        partial.setUserData("eee.workspace_id", WS)

        asyncio.run(
            _workspace_lifecycle_smoke(
                hou,
                adapter,
                root=root,
                child=child,
                alternate=alternate,
                ordinary=ordinary,
                partial=partial,
                workspace_id=WS,
                second_workspace_id=WS2,
            )
        )

        print(
            "SMOKE OK: create/set/connect applied, receipt cached, replay "
            "idempotent, trusted Workspace lifecycle read-only"
        )
    finally:
        cleanup_root = hou.node("/obj/eee_task16d_smoke_bound")
        if cleanup_root is None:
            cleanup_root = hou.node(ROOT_PATH)
        if cleanup_root is not None:
            cleanup_path = cleanup_root.path()
            cleanup_root.destroy()
            print(f"cleanup: destroyed {cleanup_path}")


if __name__ == "__main__":
    if "PYTEST" in "".join(sys.argv).upper() or "pytest" in sys.argv[0]:
        _die("this smoke runs under hython, not pytest")
    main()
