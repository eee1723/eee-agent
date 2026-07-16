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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_VENV_SITE_PACKAGES = _REPO_ROOT / ".venv" / "Lib" / "site-packages"
for _path in (_REPO_ROOT, _VENV_SITE_PACKAGES):
    if _path.exists() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    sys.exit(1)


def _safe_read(callable_, fallback: object = None) -> object:
    try:
        return callable_()
    except Exception:
        return fallback


def _houdini_fingerprint(hou) -> str:
    """Read-only full-scene fingerprint for every B2b operation."""
    root = hou.node("/")
    nodes = () if root is None else (root, *root.allSubChildren())
    inventory = []
    for node in nodes:
        parms = []
        for parm in _safe_read(node.parms, ()):
            parms.append(
                (
                    parm.name(),
                    _safe_read(parm.rawValue, "<unreadable>"),
                    _safe_read(
                        lambda parm=parm: parm.expressionLanguage().name(),
                        None,
                    ),
                )
            )
        connections = []
        for connection in _safe_read(node.inputConnections, ()):
            input_node = _safe_read(connection.inputNode, None)
            connections.append(
                (
                    _safe_read(connection.inputIndex, None),
                    _safe_read(connection.outputIndex, None),
                    None
                    if input_node is None
                    else _safe_read(input_node.path, "<unreadable>"),
                )
            )
        color = _safe_read(node.color, None)
        inventory.append(
            {
                "path": _safe_read(node.path, "<unreadable>"),
                "type": _safe_read(lambda: node.type().name(), "<unreadable>"),
                "parent": _safe_read(
                    lambda: None
                    if node.parent() is None
                    else node.parent().path(),
                    "<unreadable>",
                ),
                "parms": parms,
                "inputs": connections,
                "user_data": sorted(
                    _safe_read(node.userDataDict, {}).items()
                ),
                "position": tuple(_safe_read(node.position, ())),
                "color": None
                if color is None
                else tuple(_safe_read(color.rgb, ())),
                "comment": _safe_read(node.comment, ""),
                "selected": _safe_read(node.isSelected, None),
                "display": _safe_read(node.isDisplayFlagSet, None),
                "render": _safe_read(node.isRenderFlagSet, None),
                "template": _safe_read(node.isTemplateFlagSet, None),
                "bypass": _safe_read(node.isBypassed, None),
                "hard_locked": _safe_read(node.isHardLocked, None),
                "soft_locked": _safe_read(node.isSoftLocked, None),
            }
        )
    undo_labels = _safe_read(
        lambda: tuple(hou.undos.undoLabels()),
        None,
    )
    payload = {
        "hip_name": _safe_read(hou.hipFile.name, None),
        "hip_is_new": _safe_read(hou.hipFile.isNewFile, None),
        "undo_labels": undo_labels,
        "loaded_hdas": tuple(
            sorted(_safe_read(hou.hda.loadedFiles, ()))
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


def _select_exact(hou, nodes: tuple[object, ...]) -> None:
    hou.clearAllSelected()
    for node in nodes:
        node.setSelected(True, clear_all_selected=False)


async def _runtime_request(ws, request_id: str, command_type: str, payload: dict):
    from eee_agent.runtime.protocol import PROTOCOL, encode_envelope

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


async def _assert_b2b_no_houdini_write(
    hou,
    adapter,
    label: str,
    operation,
):
    before = _houdini_fingerprint(hou)
    epoch_before = adapter.binding().scene_epoch
    result = await operation
    after = _houdini_fingerprint(hou)
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
    run_id: str,
) -> None:
    from websockets.asyncio.client import connect

    from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest
    from eee_agent.changesets.repository import ChangeSetRepository
    from eee_agent.core import AgentException
    from eee_agent.houdini_bridge.auth import create_bridge_identity
    from eee_agent.houdini_bridge.client import BridgeClient
    from eee_agent.houdini_bridge.queue import MainThreadReadQueue
    from eee_agent.houdini_bridge.workspace_provider import (
        BridgeWorkspaceFactProvider,
    )
    from eee_agent.houdini_bridge.workspaces import WorkspaceInspectRequest
    from eee_agent.runtime.auth import create_identity
    from eee_agent.runtime.database import RuntimeDatabase
    from eee_agent.runtime.paths import RuntimePaths
    from eee_agent.runtime.server import RuntimeWebSocketServer
    from eee_agent.runtime.service import RuntimeService
    from houdini_side.secure_bridge import BridgeServer

    async def pump(queue: MainThreadReadQueue, stop: asyncio.Event) -> None:
        while not stop.is_set():
            queue.pump_one()
            await asyncio.sleep(0.002)

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
        database = await RuntimeDatabase.open(paths.app_db)
        runtime_identity = create_identity()
        service = RuntimeService(
            database,
            paths,
            workspace_fact_provider=BridgeWorkspaceFactProvider(
                paths.state_dir
            ),
        )
        runtime_server = RuntimeWebSocketServer(
            service,
            runtime_identity,
            host="127.0.0.1",
            port=0,
        )
        duplicate = None
        try:
            async with runtime_server:
                async with connect(
                    f"ws://127.0.0.1:{runtime_server.port}",
                    additional_headers={
                        "Authorization": f"Bearer {runtime_identity.token}"
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
                    now_iso = datetime.now(timezone.utc).isoformat()
                    async with database.write_transaction() as conn:
                        await conn.execute(
                            "INSERT INTO runs("
                            "run_id, session_id, status, user_input, final_response, "
                            "created_at, started_at, finished_at, failure_json, "
                            "model_snapshot_json) VALUES "
                            "(?, ?, 'Completed', 'fixture', 'done', ?, ?, ?, NULL, '{}')",
                            (
                                run_id,
                                session_id,
                                now_iso,
                                now_iso,
                                now_iso,
                            ),
                        )

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
        except AgentException as exc:
            _die(
                f"structured B2b setup failure {exc.error.code}: "
                f"{exc.error.message_for_user}"
            )
        finally:
            if duplicate is not None:
                duplicate.destroy()
            await database.close()
            stop.set()
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass
            await bridge_server.stop()
            if old_runtime_home is None:
                os.environ.pop("EEE_RUNTIME_HOME", None)
            else:
                os.environ["EEE_RUNTIME_HOME"] = old_runtime_home


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
        NodeIdentityEquals,
        NodeRef,
        OwnedNodeRef,
        ParmValueEquals,
        PermissionMode,
        RiskSummary,
        SetParm,
        WireInputEquals,
        WireRef,
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
                run_id=RUN,
            )
        )

        print(
            "SMOKE OK: create/set/connect applied, receipt cached, replay "
            "idempotent, trusted Workspace lifecycle read-only"
        )
    finally:
        if root is not None and _safe_read(root.isValid, False):
            cleanup_path = _safe_read(root.path, ROOT_PATH)
            root.destroy()
            print(f"cleanup: destroyed {cleanup_path}")
        else:
            for path in (ROOT_PATH, "/obj/eee_task16d_smoke_bound"):
                node = hou.node(path)
                if node is not None:
                    node.destroy()
                    print(f"cleanup: destroyed {path}")


if __name__ == "__main__":
    if "PYTEST" in "".join(sys.argv).upper() or "pytest" in sys.argv[0]:
        _die("this smoke runs under hython, not pytest")
    main()
