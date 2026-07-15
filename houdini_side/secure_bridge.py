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
    CHANGESET_V1,
    PreflightResponse,
    parse_preflight_request,
    validate_capabilities,
)
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    BridgeResponse,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
    parse_request,
)
from eee_agent.houdini_bridge.queue import (
    MainThreadReadQueue,
    QueueItemCancelled,
    QueueItemExpired,
    QueueRejected,
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
        if not isinstance(name, str) or not name or name == _HIP_UNSAVED_SENTINEL:
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
# Task 15-D: loopback read-only bridge server
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


class _DuplicateKeyError(ValueError):
    """Raised by the JSON object_pairs_hook on any duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


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
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


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


class _QueuedError:
    """A bounded bridge error produced by queue/operation failure.

    Carries only the structured bridge-error fields (no traceback) so the
    dispatcher can render a leak-free error envelope for either operation.
    """

    __slots__ = ("code", "category", "message_for_user", "retryable")

    def __init__(
        self, code: str, category: str, message_for_user: str, retryable: bool
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable


class BridgeServer:
    """Read-only, loopback, token-authenticated bridge server.

    Lifecycle is split: identity publication and shutdown are synchronous
    methods owned by this object; the TCP listener is owned by the host process
    (which calls ``asyncio.start_server(server.handle_connection, ...)``). The
    handler authenticates the first hello frame, parses requests through the
    frozen ``scene.query``-only :func:`parse_request`, submits the operation to
    a :class:`MainThreadReadQueue`, awaits its result, and serializes a frozen
    :class:`BridgeResponse`. It never dispatches arbitrary names, never returns a
    HOM object, and never mutates the scene.
    """

    def __init__(
        self,
        *,
        adapter: HoudiniSceneAdapter,
        identity: BridgeIdentity,
        state_dir: Path | str,
        queue: MainThreadReadQueue | None = None,
        capabilities: tuple[str, ...] = (CHANGESET_V1,),
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
                response = await self._serve(frame)
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

    async def _serve(self, frame_bytes: bytes) -> bytes:
        """Strict typed dispatch: route one request frame to its typed handler.

        Only ``scene.query`` and ``changeset.preflight`` are served in this slice.
        The operation name is read through strict JSON (rejecting malformed,
        non-UTF-8, and duplicate-key frames) and dispatched explicitly — there is
        no arbitrary name dispatch surface. Both operations share the single
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
            return await self._serve_scene_query(frame_bytes)
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
            return await self._serve_preflight(frame_bytes)
        request_id = obj.get("request_id")
        if type(request_id) is not str:
            request_id = _MALFORMED_REQUEST_ID
        return self._error_envelope(
            request_id,
            code="bridge.invalid_request",
            category="protocol",
            message_for_user="The bridge operation is not supported.",
        )

    async def _serve_scene_query(self, frame_bytes: bytes) -> bytes:
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

        result = await self._run_on_queue(request_id, request.deadline_ms, operation)
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
            )
        response = BridgeResponse(request_id=request_id, result=result, error=None)
        return response.to_json().encode("utf-8")

    async def _serve_preflight(self, frame_bytes: bytes) -> bytes:
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

        result = await self._run_on_queue(request_id, request.deadline_ms, operation)
        if isinstance(result, _QueuedError):
            return self._error_envelope(
                request_id,
                code=result.code,
                category=result.category,
                message_for_user=result.message_for_user,
                retryable=result.retryable,
            )
        response = PreflightResponse(request_id=request_id, result=result, error=None)  # type: ignore[arg-type]
        return response.to_json().encode("utf-8")

    async def _run_on_queue(
        self,
        request_id: str,
        deadline_ms: int,
        operation: "Callable[[], object]",
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
            return await future  # type: ignore[func-returns-value]
        except HoudiniAdapterError as exc:
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
