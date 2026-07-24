"""Read-only Houdini scene adapter for the secure HoudiniBridge.

This module NEVER imports ``hou`` at module import time. ``hou`` is obtained via
dependency injection (:class:`HoudiniSceneAdapter` receives a ``hou`` module) or
the :func:`create_houdini_scene_adapter` factory, which imports ``hou`` lazily
inside the Houdini process. Offline tests inject a ``fake_hou`` instead.

The adapter performs only bounded, read-only HOM reads and converts every result
into the frozen DTOs defined in :mod:`eee_agent.houdini_bridge.contracts`. It
never returns a HOM proxy, never mutates the scene, never starts a socket or a
background worker, and never imports ``rpyc`` or the Runtime.

Verified Houdini 21.0.440 API (introspected on the local install):

* ``hou.applicationVersionString()``
* ``hou.hipFile.name()`` / ``addEventCallback(callback)`` /
  ``removeEventCallback(callback)`` / ``eventCallbacks()``
* ``hou.hipFileEventType`` — ``BeforeClear``/``AfterClear``/``BeforeLoad``/
  ``AfterLoad``/``BeforeSave``/``AfterSave``/...; the callback is invoked as
  ``callback(event_type)``.
* ``hou.selectedNodes()``, ``hou.node(path)``
* ``hou.Node.path/name/type/parent``; ``hou.SopNode.isHardLocked/isSoftLocked``
  and ``geometry()``; ``hou.Geometry.pointCount/primCount/boundingBox``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from eee_agent.houdini_bridge.auth import (
    BridgeIdentity,
    remove_bridge_identity_files,
    validate_bridge_token,
    write_bridge_identity_files,
)
from eee_agent.houdini_bridge.changesets import (
    APPLY_OPERATION,
    CHANGESET_V1,
    RECEIPT_OPERATION,
    ApplyResponse,
    PreflightResponse,
    ReceiptResponse,
    parse_apply_request,
    parse_preflight_request,
    parse_receipt_request,
    validate_capabilities,
)
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    _reject_duplicate_keys,
    _DuplicateKeyError,
    PROTOCOL,
    BridgeError,
    BridgeResponse,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
    parse_request,
)
from eee_agent.houdini_bridge.capture import (
    CAPTURE_OPERATION,
    CAPTURE_V1,
    CaptureResponse,
    parse_capture_request,
)
from eee_agent.houdini_bridge.scratch import (
    SCRATCH_COMMIT_OPERATION,
    SCRATCH_DESTROY_OPERATION,
    SCRATCH_EXEC_OPERATION,
    SCRATCH_V1,
    ScratchCommitResponse,
    ScratchDestroyResponse,
    ScratchResponse,
    parse_scratch_commit_request,
    parse_scratch_destroy_request,
    parse_scratch_request,
)
from eee_agent.houdini_bridge.sensitivity import (
    SAMPLE_OPERATION,
    SENSITIVITY_V1,
    SensitivitySampleResponse,
    parse_sample_request,
)
from eee_agent.houdini_bridge.workspaces import (
    WORKSPACE_INSPECT_OPERATION,
    WORKSPACE_V1,
    WorkspaceInspectError,
    WorkspaceInspectResponse,
    parse_workspace_inspect_request,
)
from eee_agent.runtime.models import canonical_json_dumps
from eee_agent.houdini_bridge.queue import (
    AwaitSignal,
    MainThreadReadQueue,
    QueueItemCancelled,
    QueueItemExpired,
    QueueRejected,
    await_with_signal,
)

_HIP_UNSAVED_SENTINEL = "untitled"


class HoudiniAdapterError(Exception):
    """Structured, leak-free error raised by the read-only scene adapter.

    Carries the bridge error fields so the server can translate it into a
    :class:`~eee_agent.houdini_bridge.contracts.BridgeError` response. No HOM
    object, token, or traceback is embedded in the message.
    """

    __slots__ = (
        "code",
        "category",
        "message_for_user",
        "retryable",
        "technical_detail_ref",
    )

    def __init__(
        self,
        *,
        code: str,
        category: str,
        message_for_user: str,
        retryable: bool = False,
        technical_detail_ref: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable
        self.technical_detail_ref = technical_detail_ref
        super().__init__(message_for_user)


def create_houdini_scene_adapter() -> "HoudiniSceneAdapter":
    """Build an adapter bound to the real ``hou`` module (Houdini process only)."""
    import hou  # lazy: only resolved inside Houdini

    return HoudiniSceneAdapter(hou)


class HoudiniSceneAdapter:
    """Bounded, read-only view of a Houdini scene.

    ``hou`` is injected so tests can pass a ``fake_hou``. The adapter tracks a
    scene epoch starting at 1 and increments it on load/clear events only.
    """

    def __init__(self, hou: object) -> None:
        self._hou = hou
        self._scene_epoch = 1
        self._callback = None
        self._callbacks_installed = False
        self._closed = False

    # ------------------------------------------------------------------ binding

    def binding(self) -> SceneBinding:
        """Return the current scene binding (a bounded read)."""
        instance_id = self._instance_id()
        hip_path = self._hip_path()
        selection_paths = self._selection_paths()
        revision = self._revision(
            {
                "instance_id": instance_id,
                "scene_epoch": self._scene_epoch,
                "hip_path": hip_path,
                "selection": selection_paths,
            }
        )
        return SceneBinding(
            instance_id=instance_id,
            scene_epoch=self._scene_epoch,
            hip_path=hip_path,
            observed_revision=revision,
        )

    def scene_query(
        self,
        *,
        include_selection: bool,
        node_paths: Sequence[str],
        include_geometry_stats: bool,
        expected_scene_epoch: int,
    ) -> SceneQueryResult:
        """Perform one bounded, read-only scene query.

        The epoch check happens before any scene read; a mismatch raises a
        stale-scene error immediately. ``selected_nodes`` reflects
        ``hou.selectedNodes()`` at this single read; ``nodes`` reflects only the
        explicitly requested paths.
        """
        if expected_scene_epoch != self._scene_epoch:
            raise HoudiniAdapterError(
                code="bridge.stale_scene",
                category="stale_scene",
                message_for_user="The Houdini scene changed; refresh before continuing.",
                retryable=True,
            )
        instance_id = self._instance_id()
        hip_path = self._hip_path()
        selected_nodes = (
            self._read_selected(include_geometry_stats) if include_selection else ()
        )
        nodes = self._read_requested(node_paths, include_geometry_stats)
        revision = self._revision(
            {
                "instance_id": instance_id,
                "scene_epoch": self._scene_epoch,
                "hip_path": hip_path,
                "selected": [node.to_dict() for node in selected_nodes],
                "nodes": [node.to_dict() for node in nodes],
            }
        )
        binding = SceneBinding(
            instance_id=instance_id,
            scene_epoch=self._scene_epoch,
            hip_path=hip_path,
            observed_revision=revision,
        )
        return SceneQueryResult(
            binding=binding, selected_nodes=selected_nodes, nodes=nodes
        )

    # ------------------------------------------------------ scene epoch callbacks

    def install_scene_epoch_callbacks(self) -> None:
        """Register the hipFile event callback that tracks the scene epoch.

        Idempotent. The callback only bumps the epoch on load/clear; it performs
        no scene reads and no writes. On callback failure it never silently
        forges an epoch.
        """
        if self._closed or self._callbacks_installed:
            return
        # Keep a stable reference so removal uses the exact same callable.
        self._callback = self._on_scene_event
        self._hou.hipFile.addEventCallback(self._callback)
        self._callbacks_installed = True

    def _on_scene_event(self, event_type: object) -> None:
        event_types = self._hou.hipFileEventType
        # Only load/clear change the scene identity; Save/Save As must not.
        if event_type == event_types.AfterLoad or event_type == event_types.AfterClear:
            self._scene_epoch += 1

    # --------------------------------------------------------------------- close

    def close(self) -> None:
        """Release the event callback. Idempotent, non-mutating to the scene."""
        if self._closed:
            return
        self._closed = True
        if self._callbacks_installed and self._callback is not None:
            try:
                self._hou.hipFile.removeEventCallback(self._callback)
            except Exception:  # noqa: BLE001 — closing must never raise
                pass
            self._callbacks_installed = False
            self._callback = None

    # ----------------------------------------------------------- bounded readers

    def _instance_id(self) -> str:
        version = str(self._hou.applicationVersionString())
        return f"hou:{version}:pid{os.getpid()}"

    def _hip_path(self) -> str | None:
        name = self._hou.hipFile.name()
        if not isinstance(name, str) or not name:
            return None
        # An unsaved scene reports some ``untitled[.hip]`` variant (often a
        # full temp path); match on the basename stem, not exact equality.
        stem = name.replace("\\", "/").rsplit("/", 1)[-1]
        if stem == _HIP_UNSAVED_SENTINEL or stem == _HIP_UNSAVED_SENTINEL + ".hip":
            return None
        return name

    def _selection_paths(self) -> list[str]:
        paths: list[str] = []
        for node in self._hou.selectedNodes():
            try:
                paths.append(str(node.path()))
            except Exception:  # noqa: BLE001 — skip a node we cannot read
                continue
        return paths

    def _read_selected(
        self, include_geometry_stats: bool
    ) -> tuple[SelectedNode, ...]:
        nodes = []
        for node in self._hou.selectedNodes():
            nodes.append(self._to_selected_node(node, include_geometry_stats))
        return tuple(nodes)

    def _read_requested(
        self, node_paths: Sequence[str], include_geometry_stats: bool
    ) -> tuple[SelectedNode, ...]:
        nodes = []
        for path in node_paths:
            if not (isinstance(path, str) and path.startswith("/")):
                raise HoudiniAdapterError(
                    code="bridge.invalid_request",
                    category="protocol",
                    message_for_user="Requested node paths must be absolute Houdini paths.",
                )
            node = self._hou.node(path)
            if node is None:
                raise HoudiniAdapterError(
                    code="bridge.houdini_read_failed",
                    category="houdini_read",
                    message_for_user="A requested node was not found in the scene.",
                )
            nodes.append(self._to_selected_node(node, include_geometry_stats))
        return tuple(nodes)

    def _to_selected_node(
        self, node: object, include_geometry_stats: bool
    ) -> SelectedNode:
        path = str(node.path())
        node_type = str(node.type().name())
        parent = node.parent()
        parent_path = str(parent.path()) if parent is not None else "/"
        display_name = str(node.name())
        is_locked = self._read_is_locked(node)
        geometry_stats = (
            self._read_geometry_stats(node) if include_geometry_stats else None
        )
        return SelectedNode(
            path=path,
            node_type=node_type,
            parent_path=parent_path,
            display_name=display_name,
            is_locked=is_locked,
            geometry_stats=geometry_stats,
        )

    def _read_is_locked(self, node: object) -> bool:
        # isHardLocked / isSoftLocked exist on hou.SopNode; other nodes have no
        # lock concept. This is a bounded read, not arbitrary attribute access.
        try:
            return bool(node.isHardLocked() or node.isSoftLocked())
        except Exception:  # noqa: BLE001 — non-SOP nodes simply aren't locked
            return False

    def _read_geometry_stats(self, node: object) -> dict[str, object] | None:
        try:
            geometry = node.geometry()
        except Exception:  # noqa: BLE001 — node has no cookable geometry
            return None
        try:
            points = int(geometry.pointCount())
            prims = int(geometry.primCount())
        except Exception:  # noqa: BLE001 — geometry unreadable
            return None
        stats: dict[str, object] = {"points": points, "primitives": prims}
        bbox = self._read_bbox(geometry)
        if bbox is not None:
            stats["bbox"] = bbox
        return stats

    def _read_bbox(self, geometry: object) -> dict[str, list[float]] | None:
        try:
            bbox = geometry.boundingBox()
            mn = bbox.minvec()
            mx = bbox.maxvec()
        except Exception:  # noqa: BLE001 — empty geometry has no bounding box
            return None
        return {
            "min": [mn.x(), mn.y(), mn.z()],
            "max": [mx.x(), mx.y(), mx.z()],
        }

    # ------------------------------------------------------------- revision hash

    def _revision(self, facts: dict[str, object]) -> str:
        """Deterministic read hash over bounded scene facts (evidence only)."""
        text = json.dumps(
            facts,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ==========================================================================
# Loopback, token-authenticated bridge server
#
# The connection handler is a pure ``async def`` over asyncio reader/writer
# objects. It deliberately imports neither ``asyncio`` nor ``threading`` and
# never binds a socket itself: the owning process (the Houdini main thread, or
# a test) calls ``asyncio.start_server(server.handle_connection, host, port)``
# and drives ``queue.pump_one()`` from its main-thread event callback. The
# network loop performs transport I/O only; every HOM read happens inside the
# queue operation, on the pump thread. Only ``scene.query`` is served; no HOM
# object is ever returned and the HIP is never mutated.
# ==========================================================================


_HEADER_LEN = 4
# Sentinel request_id for envelopes where the inbound frame could not be parsed
# (the real request_id is unknowable). A well-formed client always sends a valid
# request_id; a mismatch surfaces as bridge.invalid_request client-side.
_MALFORMED_REQUEST_ID = "_bridge_malformed"


class _ConnectionClosed(Exception):
    """The client closed the connection (EOF / socket error)."""


class _FrameError(Exception):
    """A frame length is invalid (zero / oversize) — reject before the payload."""


def _loads_object(data: bytes) -> dict[str, object] | None:
    """Strictly decode a frame to a dict, or ``None`` if it is not valid."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateKeyError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _canonical_dumps(obj: object) -> str:
    return canonical_json_dumps(obj)


async def _await_listener_closed(listener: object) -> None:
    """Best-effort ``listener.close()`` + ``await listener.wait_closed()``.

    Swallows every error so a failing cleanup can never mask the original
    publication/shutdown error. Works with both stdlib asyncio and Houdini's
    ``haio`` listener (whose close/wait_closed are non-stdlib). This helper does
    NOT import ``asyncio`` — it only calls methods on the passed listener.
    """
    try:
        listener.close()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    try:
        await listener.wait_closed()  # type: ignore[func-returns-value]
    except Exception:  # noqa: BLE001
        pass


def _make_preflight_adapter(adapter: "HoudiniSceneAdapter") -> object:
    """Lazily build the read-only preflight adapter.

    Imported lazily so ``secure_bridge`` and ``changeset_executor`` do not form a
    top-level import cycle (each test/entrypoint may import either first).
    """
    from houdini_side.changeset_executor import ChangeSetPreflightAdapter

    return ChangeSetPreflightAdapter(adapter)


def _make_executor(adapter: "HoudiniSceneAdapter") -> object:
    """Lazily build the transactional ChangeSet executor (Task 16-D)."""
    from houdini_side.changeset_executor import ChangeSetExecutor

    return ChangeSetExecutor(adapter)


def _make_workspace_inspector(adapter: "HoudiniSceneAdapter") -> object:
    """Lazily build the read-only stable-ID workspace inspector."""
    from houdini_side.workspace_inspector import WorkspaceInspector

    return WorkspaceInspector(adapter._hou, binding_provider=adapter.binding)


class _QueuedError:
    """A bounded bridge error produced by queue/operation failure.

    Carries only the structured bridge-error fields (no traceback) so the
    dispatcher can render a leak-free error envelope for either operation.
    """

    __slots__ = (
        "code",
        "category",
        "message_for_user",
        "retryable",
        "technical_detail_ref",
    )

    def __init__(
        self,
        code: str,
        category: str,
        message_for_user: str,
        retryable: bool,
        technical_detail_ref: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable
        self.technical_detail_ref = technical_detail_ref


class BridgeServer:
    """Loopback, token-authenticated bridge server.

    Lifecycle is split: identity publication and shutdown are synchronous
    methods owned by this object; the TCP listener is owned by the host process
    (which calls ``asyncio.start_server(server.handle_connection, ...)``). The
    handler authenticates the first hello frame, dispatches only explicit typed
    operations — ``scene.query``, ``workspace.inspect``,
    ``changeset.preflight/apply/receipt``, ``sensitivity.sample``,
    ``capture.capture``, and ``scratch.exec/commit/destroy`` — submits every
    operation to one :class:`MainThreadReadQueue`, awaits its result, and
    serializes a frozen response DTO. It never dispatches arbitrary names or
    returns a HOM object. Mutating operations (changeset apply, scratch
    exec/commit/destroy) run on the same serialized FIFO as the reads, so HOM
    access never interleaves.
    """

    def __init__(
        self,
        *,
        adapter: HoudiniSceneAdapter,
        identity: BridgeIdentity,
        state_dir: Path | str,
        queue: MainThreadReadQueue | None = None,
        capabilities: tuple[str, ...] = (
            CAPTURE_V1,
            CHANGESET_V1,
            SCRATCH_V1,
            SENSITIVITY_V1,
            WORKSPACE_V1,
        ),
    ) -> None:
        if not isinstance(adapter, HoudiniSceneAdapter):
            raise TypeError("adapter must be a HoudiniSceneAdapter")
        if not isinstance(identity, BridgeIdentity):
            raise TypeError("identity must be a BridgeIdentity")
        # Capabilities are advertised on a successful hello ack. They are an
        # exact, sorted, unique list (validated here so a misconfigured server
        # fails loudly instead of advertising a malformed set).
        self._capabilities = validate_capabilities(list(capabilities))
        self._adapter = adapter
        self._identity = identity
        self._state_dir = Path(state_dir)
        self._queue = queue if queue is not None else MainThreadReadQueue()
        self._preflight = _make_preflight_adapter(adapter)
        self._executor = _make_executor(adapter)
        self._workspace_inspector = _make_workspace_inspector(adapter)
        self._closed = False
        self._writers: list[object] = []
        self._listener: object | None = None
        self._ready = False

    @property
    def capabilities(self) -> tuple[str, ...]:
        """The exact, sorted capabilities advertised on a successful hello."""
        return self._capabilities

    @property
    def is_serving(self) -> bool:
        """True only while an adopted listener is actively serving."""
        listener = self._listener
        if listener is None:
            return False
        try:
            return bool(listener.is_serving())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a non-stdlib listener still counts as not serving
            return False

    # -- identity publication ------------------------------------------------

    def publish_identity(self, *, host: str, port: int) -> None:
        """Atomically publish ``bridge.token`` then discovery to ``state_dir``.

        Raises (and leaves no partial identity file) if either publication
        fails. Low-level hook for callers that manage their own listener; most
        callers should use :meth:`serve` instead, which guarantees the listener
        is closed on publication failure.
        """
        if self._closed:
            raise RuntimeError("BridgeServer is closed")
        write_bridge_identity_files(
            self._identity, self._state_dir, host=host, port=port
        )

    # -- startup lifecycle seam ----------------------------------------------

    async def serve(self, listener: object, *, host: str) -> int:
        """Adopt a bound listener, publish identity atomically, mark ready.

        The host creates ``listener`` (``asyncio.start_server(...)``) and passes
        it here. Identity publication happens AFTER the bind so discovery can
        advertise the real port. The strong guarantee: if publication fails, the
        listener is closed and awaited, the identity files are removed, and the
        ORIGINAL publication error propagates — cleanup errors never mask it.
        Only after successful publication is the server ``ready``. Returns the
        bound port.

        This method does not import ``asyncio``; it only calls methods on the
        passed listener, so the Task 15-C structural import checks are preserved.
        """
        if self._closed:
            raise RuntimeError("BridgeServer is closed")
        if self._listener is not None:
            raise RuntimeError("BridgeServer is already serving a listener")
        bound_port = listener.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
        self._listener = listener
        try:
            self.publish_identity(host=host, port=bound_port)
        except BaseException:
            # Guarantee: the listener we adopted is closed + awaited first, then
            # the rest of the server is cleaned up. The original error is re-raised;
            # any cleanup failure is swallowed so it cannot mask the original.
            await _await_listener_closed(self._listener)
            try:
                self.close()
            except Exception:  # noqa: BLE001 — never mask the original failure
                pass
            raise
        self._ready = True
        return bound_port

    # -- shutdown ------------------------------------------------------------

    def close(self) -> None:
        """Stop accepting, drain the queue, close writers, remove files/callback.

        Idempotent. Synchronously closes the owned listener (use :meth:`stop` to
        also ``await wait_closed``). Does not save, clear, mutate, or export the
        HIP.
        """
        if self._closed:
            return
        self._closed = True
        self._ready = False
        # 1. Stop accepting new connections on the owned listener.
        if self._listener is not None:
            try:
                self._listener.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        # 2. Drain/reject queued work so no handler stays awaiting a future.
        self._queue.shutdown()
        # 2b. Let an already-running transaction finish before touching the
        # adapter, identity files, or writers. After shutdown, pending_count can
        # only represent the one synchronous item already running on the Houdini
        # pump thread; queued items were rejected above. Typed Bridge operations
        # cannot call close(), so this wait cannot re-enter from that pump item.
        while self._queue.pending_count:
            time.sleep(0.001)
        # 3. Close tracked connection writers so handlers wind down.
        for writer in list(self._writers):
            try:
                writer.close()  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001 — closing must never raise
                pass
        self._writers = []
        # 4. Remove the identity handoff files.
        remove_bridge_identity_files(self._state_dir)
        # 5. Release the scene-epoch callback (non-mutating to the scene).
        self._adapter.close()

    async def stop(self) -> None:
        """Full async shutdown: :meth:`close` then ``await listener.wait_closed()``.

        Idempotent. Ordered: stop accepting, drain/reject queued work, close
        connection writers, remove identity files, remove the Houdini epoch
        callback, then await the listener's full close. Does not mutate the HIP.
        """
        listener = self._listener
        self.close()
        if listener is not None:
            await _await_listener_closed(listener)
            self._listener = None

    # -- connection handler --------------------------------------------------

    async def handle_connection(self, reader: object, writer: object) -> None:
        """Serve one loopback connection: hello handshake then request loop."""
        if self._closed:
            self._safe_close(writer)
            return
        self._writers.append(writer)
        try:
            if not await self._handshake(reader, writer):
                return  # auth/protocol failure: ack already sent, then close
            while not self._closed:
                try:
                    frame = await self._recv_frame(reader)
                except _ConnectionClosed:
                    return
                except _FrameError:
                    await self._send(
                        writer,
                        self._error_envelope(
                            _MALFORMED_REQUEST_ID,
                            code="bridge.invalid_request",
                            category="protocol",
                            message_for_user="The bridge request frame is invalid.",
                        ),
                    )
                    return
                response = await self._serve(frame, reader=reader)
                await self._send(writer, response)
        finally:
            self._safe_close(writer)
            if writer in self._writers:
                self._writers.remove(writer)

    async def _handshake(self, reader: object, writer: object) -> bool:
        """Read + validate the hello frame; send the ack. Returns auth success."""
        try:
            hello_bytes = await self._recv_frame(reader)
        except (_ConnectionClosed, _FrameError):
            return False
        ok = self._validate_hello(hello_bytes)
        # A successful ack advertises the exact, sorted capability list so a new
        # client can gate write-style operations (changeset.preflight). A failed
        # ack carries only the protocol/kind/ok fields.
        ack: dict[str, object] = {"protocol": PROTOCOL, "kind": "hello", "ok": ok}
        if ok:
            ack["capabilities"] = list(self._capabilities)
        await self._send(writer, _canonical_dumps(ack).encode("utf-8"))
        return ok

    def _validate_hello(self, hello_bytes: bytes) -> bool:
        obj = _loads_object(hello_bytes)
        if obj is None:
            return False
        if obj.get("protocol") != PROTOCOL:
            return False
        if obj.get("kind") != "hello":
            return False
        return validate_bridge_token(self._identity, obj.get("token"))

    async def _serve(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Strict typed dispatch: route one request frame to its typed handler.

        Accepted operations are ``scene.query``, ``workspace.inspect``, the
        typed ChangeSet preflight/apply/receipt handlers, ``sensitivity.sample``,
        ``capture.capture``, and the ``scratch.exec/commit/destroy`` sandbox
        handlers.
        The operation name is read through strict JSON (rejecting malformed,
        non-UTF-8, and duplicate-key frames) and dispatched explicitly — there is
        no arbitrary name dispatch surface. All operations share the single
        bounded main-thread FIFO so HOM access never interleaves with a write.
        """
        obj = _loads_object(frame_bytes)
        if obj is None:
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The bridge request is not valid.",
            )
        operation = obj.get("operation")
        if operation == "scene.query":
            return await self._serve_scene_query(frame_bytes, reader=reader)
        if operation == WORKSPACE_INSPECT_OPERATION:
            if WORKSPACE_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support workspace inspection.",
                )
            return await self._serve_workspace_inspect(frame_bytes, reader=reader)
        if operation == "changeset.preflight":
            # Admission: an old server that does not advertise changeset.v1 must
            # fail closed for preflight BEFORE any HOM access or payload parsing.
            if CHANGESET_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support changeset preflight.",
                )
            return await self._serve_preflight(frame_bytes, reader=reader)
        if operation == APPLY_OPERATION:
            if CHANGESET_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support changeset apply.",
                )
            return await self._serve_apply(frame_bytes, reader=reader)
        if operation == RECEIPT_OPERATION:
            if CHANGESET_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support changeset receipt queries.",
                )
            return await self._serve_receipt(frame_bytes, reader=reader)
        if operation == SAMPLE_OPERATION:
            # Admission: a server that does not advertise sensitivity.v1 must
            # fail closed BEFORE any HOM access or payload parsing.
            if SENSITIVITY_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support sensitivity sampling.",
                )
            return await self._serve_sample_sensitivity(frame_bytes, reader=reader)
        if operation == CAPTURE_OPERATION:
            # Admission: a server that does not advertise capture.v1 must
            # fail closed BEFORE any HOM access or payload parsing.
            if CAPTURE_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support artifact capture.",
                )
            return await self._serve_capture(frame_bytes, reader=reader)
        if operation == SCRATCH_EXEC_OPERATION:
            # Admission: a server that does not advertise scratch.v1 must fail
            # closed BEFORE any HOM access or payload parsing.
            if SCRATCH_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support scratch sandbox operations.",
                )
            return await self._serve_scratch_exec(frame_bytes, reader=reader)
        if operation == SCRATCH_COMMIT_OPERATION:
            # Same scratch.v1 capability gates commit; a server without it
            # fails closed BEFORE any HOM access or payload parsing.
            if SCRATCH_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support scratch sandbox operations.",
                )
            return await self._serve_scratch_commit(frame_bytes, reader=reader)
        if operation == SCRATCH_DESTROY_OPERATION:
            # destroy is gated on scratch.v1 but BYPASSES the write-freeze gate:
            # cleanup MUST run even after an uncertain recovery, otherwise a
            # crashed run leaks its sandbox container forever.
            if SCRATCH_V1 not in self._capabilities:
                request_id = obj.get("request_id")
                if type(request_id) is not str:
                    request_id = _MALFORMED_REQUEST_ID
                return self._error_envelope(
                    request_id,
                    code="bridge.capability_unavailable",
                    category="capability",
                    message_for_user="The bridge does not support scratch sandbox operations.",
                )
            return await self._serve_scratch_destroy(frame_bytes, reader=reader)
        request_id = obj.get("request_id")
        if type(request_id) is not str:
            request_id = _MALFORMED_REQUEST_ID
        return self._error_envelope(
            request_id,
            code="bridge.invalid_request",
            category="protocol",
            message_for_user="The bridge operation is not supported.",
        )

    async def _serve_scene_query(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a scene.query request; return the response envelope bytes."""
        try:
            request = parse_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The bridge request is not valid.",
            )
        request_id = request.request_id
        payload = request.payload
        include_selection = bool(payload.get("include_selection", False))
        include_geometry_stats = bool(payload.get("include_geometry_stats", False))
        node_paths = list(payload.get("node_paths", ()))
        expected_scene_epoch = request.scene_epoch

        def operation() -> SceneQueryResult:
            return self._adapter.scene_query(
                include_selection=include_selection,
                node_paths=node_paths,
                include_geometry_stats=include_geometry_stats,
                expected_scene_epoch=expected_scene_epoch,
            )

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = BridgeResponse(request_id=request_id, result=result, error=None)
        return response.to_json().encode("utf-8")

    async def _serve_workspace_inspect(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse and serialize one read-only workspace inspection on the FIFO."""
        try:
            request = parse_workspace_inspect_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The workspace inspection request is not valid.",
            )
        request_id = request.request_id

        def operation() -> object:
            return self._workspace_inspector.inspect(request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = WorkspaceInspectResponse(
            request_id=request_id,
            result=result,  # type: ignore[arg-type]
            error=None,
        )
        return response.to_json().encode("utf-8")

    async def _serve_preflight(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a changeset.preflight request; return the response bytes.

        Parsing validates the full canonical ChangeSet, its digest, manifest
        identity/binding consistency, exact fields, duplicate keys, and size
        BEFORE the main-thread callable runs. The callable performs only
        read-only scene fact gathering through the shared FIFO.
        """
        try:
            request = parse_preflight_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The preflight request is not valid.",
            )
        request_id = request.request_id
        preflight_request = request

        def operation() -> object:
            return self._preflight.preflight(preflight_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = PreflightResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_apply(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``changeset.apply`` request; return the receipt bytes.

        Admission checks the advertised capability and the write-freeze state
        BEFORE the main-thread transaction runs. Pre-transaction failures (stale
        scene/manifest/identity, locked target, a non-holding precondition, or a
        digest conflict) surface as a structured bridge error and perform zero
        writes. Transaction outcomes (Applied/RolledBack/Partial/CriticalRecovery)
        are returned as a typed :class:`ChangeReceipt`.
        """
        try:
            request = parse_apply_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The apply request is not valid.",
            )
        request_id = request.request_id
        if self._executor.write_frozen:  # type: ignore[attr-defined]
            return self._error_envelope(
                request_id,
                code="bridge.write_frozen",
                category="write_frozen",
                message_for_user=(
                    "The bridge is frozen for writes after an uncertain recovery."
                ),
                retryable=False,
            )
        apply_request = request

        def operation() -> object:
            return self._executor.apply(apply_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ApplyResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_receipt(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``changeset.receipt`` request; return the receipt bytes.

        A receipt query only reads the process-local receipt cache; it never
        touches or mutates the scene. A not-found id/digest is a bounded,
        retryable ``changeset.receipt_unavailable`` error. The cache lookup is
        serialized through the shared FIFO so it cannot race an in-flight apply.
        """
        try:
            request = parse_receipt_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The receipt request is not valid.",
            )
        request_id = request.request_id
        receipt_request = request

        def operation() -> object:
            return self._executor.receipt(  # type: ignore[union-attr]
                receipt_request.change_id,
                receipt_request.changeset_digest,
                scene_epoch=receipt_request.scene_epoch,
            )

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        if result is None:
            return self._error_envelope(
                request_id,
                code="changeset.receipt_unavailable",
                category="not_found",
                message_for_user="No receipt is cached for this change id and digest.",
                retryable=True,
            )
        response = ReceiptResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_sample_sensitivity(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``sensitivity.sample`` request; return evidence bytes.

        Admission checks the advertised capability and the write-freeze state
        BEFORE the main-thread cycle runs, exactly like ``changeset.apply`` —
        the sample-and-restore cycle writes parameter values, so an uncertain
        earlier recovery must gate it. Pre-write failures (stale scene, an
        unresolvable target, a missing parameter) surface as a structured
        bridge error with zero writes. Cook failures, aborted captures, and
        restore failures are classified by the executor; an unverifiable
        restore freezes writes and never produces a result envelope.
        """
        try:
            request = parse_sample_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The sample request is not valid.",
            )
        request_id = request.request_id
        if self._executor.write_frozen:  # type: ignore[attr-defined]
            return self._error_envelope(
                request_id,
                code="bridge.write_frozen",
                category="write_frozen",
                message_for_user=(
                    "The bridge is frozen for writes after an uncertain recovery."
                ),
                retryable=False,
            )
        sample_request = request

        def operation() -> object:
            return self._executor.sample_sensitivity(sample_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = SensitivitySampleResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_capture(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``capture.capture`` request; return reference bytes.

        Admission checks the advertised capability BEFORE the main-thread
        operation runs. The op is a read+capture that creates only an owned
        temp camera/ROP scope (cleaned up in all cases, including on render
        failure), so the write-freeze gate does not apply — but the executor's
        stale-epoch precheck and never-guess discipline do. Pre-scope failures
        (stale scene, unavailable target directory, unresolvable node, cook or
        framing failure) surface as a structured bridge error with zero scene
        changes; a render or cleanup failure is classified and never produces
        a guessed reference. Only content-addressed reference fields are
        returned — image bytes never cross the wire.
        """
        try:
            request = parse_capture_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The capture request is not valid.",
            )
        request_id = request.request_id
        capture_request = request

        def operation() -> object:
            return self._executor.capture(capture_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = CaptureResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_scratch_exec(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``scratch.exec`` request; return bounded diagnostics.

        Scratch builds nodes inside a reserved ``/obj/eee_scratch_<id>``
        container (no ownership mirrors), so it IS a scene write and inherits
        the write-freeze gate (unlike capture, which only touches an owned
        temp scope that is always cleaned up). A frozen bridge refuses the
        sandbox op before any HOM access. The executor creates the container
        if absent, applies the structured ops in one undo group, collects
        diagnostics (cooked geometry / errors of the output node), and
        preserves the sandbox on failure so the agent can inspect and retry.
        """
        try:
            request = parse_scratch_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The scratch request is not valid.",
            )
        request_id = request.request_id
        if self._executor.write_frozen:  # type: ignore[attr-defined]
            return self._error_envelope(
                request_id,
                code="bridge.write_frozen",
                category="write_frozen",
                message_for_user=(
                    "The bridge is frozen for writes after an uncertain recovery."
                ),
                retryable=False,
            )
        scratch_request = request

        def operation() -> object:
            return self._executor.scratch_exec(scratch_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ScratchResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_scratch_commit(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``scratch.commit`` request; return the verdict.

        Commit promotes a verified sandbox into the real scene through the
        four hard verify gates. Like scratch.exec it IS a scene write, so it
        inherits the write-freeze gate. The executor runs the gates, and on
        pass renames the sandbox into the target path inside one undo group
        (atomic rollback on partial failure). On refusal the sandbox is
        preserved so the agent can fix and re-commit.
        """
        try:
            request = parse_scratch_commit_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The scratch commit request is not valid.",
            )
        request_id = request.request_id
        if self._executor.write_frozen:  # type: ignore[attr-defined]
            return self._error_envelope(
                request_id,
                code="bridge.write_frozen",
                category="write_frozen",
                message_for_user=(
                    "The bridge is frozen for writes after an uncertain recovery."
                ),
                retryable=False,
            )
        commit_request = request

        def operation() -> object:
            return self._executor.scratch_commit(commit_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ScratchCommitResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _serve_scratch_destroy(
        self, frame_bytes: bytes, *, reader: object | None = None
    ) -> bytes:
        """Parse + queue a ``scratch.destroy`` request; return the result.

        Best-effort sandbox cleanup. Unlike exec/commit, destroy BYPASSES the
        write-freeze gate: cleanup must run even after an uncertain recovery,
        otherwise a crashed run leaks its sandbox container forever. The
        executor destroys the ``/obj/eee_scratch_<sandbox_id>`` container if it
        exists and returns ``missing=True`` if it does not.
        """
        try:
            request = parse_scratch_destroy_request(frame_bytes)
        except (TypeError, ValueError):
            return self._error_envelope(
                _MALFORMED_REQUEST_ID,
                code="bridge.invalid_request",
                category="protocol",
                message_for_user="The scratch destroy request is not valid.",
            )
        request_id = request.request_id
        # NOTE: intentionally NO write-frozen gate here.
        destroy_request = request

        def operation() -> object:
            return self._executor.scratch_destroy(destroy_request)  # type: ignore[union-attr]

        result = await self._run_on_queue(
            request_id, request.deadline_ms, operation, reader=reader
        )
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
                technical_detail_ref=result.technical_detail_ref,
            )
        response = ScratchDestroyResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _run_on_queue(
        self,
        request_id: str,
        deadline_ms: int,
        operation: "Callable[[], object]",
        reader: object | None = None,
    ) -> object:
        """Submit one operation to the shared FIFO and map queue failures.

        Returns the operation result, or a :class:`_QueuedError` carrying a
        bounded bridge error code (no traceback is ever leaked to the client).
        """
        deadline_monotonic = time.monotonic() + deadline_ms / 1000.0
        try:
            future = self._queue.submit(
                request_id, operation, deadline_monotonic=deadline_monotonic
            )
            return await self._await_queued(  # type: ignore[func-returns-value]
                reader, future, request_id
            )
        except HoudiniAdapterError as exc:
            return _QueuedError(
                exc.code,
                exc.category,
                exc.message_for_user,
                exc.retryable,
                exc.technical_detail_ref,
            )
        except WorkspaceInspectError as exc:
            return _QueuedError(
                exc.code, exc.category, exc.message_for_user, exc.retryable
            )
        except QueueItemExpired:
            return _QueuedError(
                "bridge.deadline_exceeded",
                "deadline",
                "The bridge request exceeded its deadline.",
                True,
            )
        except QueueItemCancelled:
            return _QueuedError(
                "bridge.cancelled",
                "cancelled",
                "The bridge request was cancelled.",
                False,
            )
        except QueueRejected:
            return _QueuedError(
                "bridge.not_available",
                "not_available",
                "The bridge is no longer available.",
                True,
            )
        except Exception:  # noqa: BLE001 — never leak a traceback to the client
            return _QueuedError(
                "bridge.internal_failure",
                "internal",
                "The bridge encountered an internal failure.",
                False,
            )

    async def _await_queued(
        self, reader: object | None, future: object, request_id: str
    ) -> object:
        """Await the queued result, cancelling it if the client goes away.

        The wait is polled through :func:`await_with_signal` (no tasks,
        no threads, no ``asyncio`` import in this module): between short
        timer slices a proactively delivered transport EOF is visible
        through ``reader.at_eof()`` and the queued item is cancelled, so
        main-thread HOM work is never spent on a dead client. A queued
        item is removed without running; a running item finishes but its
        result is discarded (the Runtime recovers through the durable
        receipt path). No inbound byte is ever consumed, so sequential
        request/response traffic is unaffected. A reader without EOF
        introspection falls back to the plain await.
        """
        at_eof = getattr(reader, "at_eof", None)
        if reader is None or not callable(at_eof):
            return await future
        outcome = await await_with_signal(future, at_eof)
        if outcome is AwaitSignal.SIGNALLED:
            # The client is gone; cancel resolves the queue future as
            # QueueItemCancelled, which the plain await below observes.
            self._queue.cancel(request_id)
        return await future


    # -- framed transport (reader/writer only; no asyncio import) ------------

    async def _recv_frame(self, reader: object) -> bytes:
        """Read one length-prefixed frame, rejecting bad lengths before payload."""
        try:
            header = await reader.readexactly(_HEADER_LEN)  # type: ignore[union-attr]
        except (EOFError, OSError):
            raise _ConnectionClosed()
        length = int.from_bytes(header, "big")
        if length <= 0 or length > MAX_MESSAGE_BYTES:
            raise _FrameError("invalid frame length")
        try:
            payload = await reader.readexactly(length)  # type: ignore[union-attr]
        except (EOFError, OSError):
            raise _ConnectionClosed()
        return payload

    async def _send(self, writer: object, payload_bytes: bytes) -> None:
        header = len(payload_bytes).to_bytes(_HEADER_LEN, "big")
        writer.write(header + payload_bytes)  # type: ignore[union-attr]
        try:
            await writer.drain()  # type: ignore[union-attr]
        except OSError:
            pass

    def _safe_close(self, writer: object) -> None:
        try:
            writer.close()  # type: ignore[call-arg]
        except Exception:  # noqa: BLE001 — closing must never raise
            pass

    def _error_envelope(
        self,
        request_id: str,
        *,
        code: str,
        category: str,
        message_for_user: str,
        retryable: bool = False,
        technical_detail_ref: str | None = None,
    ) -> bytes:
        response = BridgeResponse(
            request_id=request_id,
            result=None,
            error=BridgeError(
                code=code,
                category=category,
                message_for_user=message_for_user,
                retryable=retryable,
                technical_detail_ref=technical_detail_ref,
            ),
        )
        return response.to_json().encode("utf-8")
