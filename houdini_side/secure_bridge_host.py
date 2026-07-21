"""Houdini-owned lifecycle for the authenticated typed Secure Bridge.

Transport I/O runs on one background asyncio thread. Every Houdini read/write
operation still runs through the accepted single ``MainThreadReadQueue``, which
is pumped by ``hou.ui.addEventLoopCallback`` on Houdini's main thread.

The module also exposes the Task 17-A2 read-only selection query used by the
panel. That query obtains a nullable-epoch binding through ``workspace.inspect``
and only then issues ``scene.query`` with the exact returned scene epoch.
"""

from __future__ import annotations

import asyncio
import atexit
import sys
import threading
import uuid
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from eee_agent.houdini_bridge.auth import (  # noqa: E402
    BridgeIdentityError,
    BridgeTokenError,
    create_bridge_identity,
)
from eee_agent.houdini_bridge.client import (  # noqa: E402
    BridgeClient,
    BridgeClientError,
)
from eee_agent.houdini_bridge.contracts import (  # noqa: E402
    BridgeOperation,
    BridgeRequest,
    SceneQueryResult,
)
from eee_agent.houdini_bridge.queue import MainThreadReadQueue  # noqa: E402
from eee_agent.houdini_bridge.workspaces import (  # noqa: E402
    WorkspaceInspectRequest,
)
from eee_agent.panel.client_state import runtime_state_dir  # noqa: E402
from houdini_side.secure_bridge import (  # noqa: E402
    BridgeServer,
    HoudiniSceneAdapter,
    create_houdini_scene_adapter,
)

_HOST = "127.0.0.1"
_DEFAULT_DEADLINE_MS = 5000
_START_TIMEOUT_SECONDS = 10.0
_JOIN_TIMEOUT_SECONDS = 10.0
_PUMP_BUDGET = 8
_T = TypeVar("_T")


class SecureBridgeHostError(RuntimeError):
    """Bounded lifecycle failure; never contains either bearer token."""


class SelectionQueryError(RuntimeError):
    """Bounded selection-inspection failure safe to display in the panel."""

    __slots__ = ("code", "retryable")

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


def _new_background_event_loop() -> asyncio.AbstractEventLoop:
    """Create a real thread-local loop without Houdini's main-thread policy.

    Houdini installs ``haio.HoudiniEventLoopPolicy`` process-wide. Its
    ``new_event_loop()`` always returns the singleton Houdini UI loop, whose
    task creation deliberately fails outside the main thread. The Secure Bridge
    transport and panel client instead need an ordinary socket-capable loop in
    their worker threads, so construct the stdlib selector loop directly rather
    than consulting the active policy.
    """
    factory = getattr(asyncio, "SelectorEventLoop", None)
    if factory is None:
        raise SecureBridgeHostError(
            "A background asyncio event loop is not available."
        )
    return factory()


def run_background_async(factory: Callable[[], Awaitable[_T]]) -> _T:
    """Run one async operation on an isolated stdlib loop.

    ``asyncio.run`` cannot be used in a Houdini worker thread because it asks
    the installed ``haio`` policy for a loop. Creating the awaitable lazily also
    avoids leaking an un-awaited coroutine if loop construction fails.
    """
    loop = _new_background_event_loop()
    try:
        return loop.run_until_complete(factory())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        except Exception:
            pass
        loop.close()


def _request_id(label: str) -> str:
    return f"panel_{label}_{uuid.uuid4().hex}"


async def query_selection(
    state_dir: Path | str,
    *,
    deadline_ms: int = _DEFAULT_DEADLINE_MS,
    client_factory: Callable[[Path], BridgeClient] | None = None,
) -> SceneQueryResult:
    """Return exact typed facts for the current Houdini selection.

    A short-lived authenticated client performs:

    1. ``workspace.inspect(mode=selection, scene_epoch=null)`` for a current
       binding;
    2. ``scene.query`` with that exact non-null epoch.

    If the scene changes between those reads, the complete two-read cycle is
    retried once. There is no wildcard or unbound ``scene.query`` fallback.
    """
    if type(deadline_ms) is not int or not 1 <= deadline_ms <= 30_000:
        raise ValueError("deadline_ms must be an exact integer in 1..30000")
    state = Path(state_dir)
    factory = client_factory or BridgeClient.from_state_dir

    for attempt in range(2):
        try:
            client = factory(state)
            async with asyncio.timeout(deadline_ms / 1000.0):
                async with client:
                    binding_result = await client.inspect_workspace(
                        WorkspaceInspectRequest(
                            request_id=_request_id("binding"),
                            deadline_ms=deadline_ms,
                            scene_epoch=None,
                            mode="selection",
                            manifest=None,
                        )
                    )
                    binding = binding_result.binding
                    result = await client.request(
                        BridgeRequest(
                            request_id=_request_id("selection"),
                            operation=BridgeOperation.SCENE_QUERY,
                            deadline_ms=deadline_ms,
                            scene_epoch=binding.scene_epoch,
                            payload={
                                "include_selection": True,
                                "node_paths": [],
                                "include_geometry_stats": True,
                            },
                        )
                    )
            if (
                result.binding.instance_id != binding.instance_id
                or result.binding.scene_epoch != binding.scene_epoch
            ):
                raise SelectionQueryError(
                    "bridge.binding_mismatch",
                    "The Secure Bridge returned inconsistent scene identity.",
                    retryable=True,
                )
            return result
        except BridgeClientError as exc:
            if exc.code == "bridge.stale_scene" and attempt == 0:
                continue
            raise SelectionQueryError(
                exc.code,
                exc.message_for_user,
                retryable=exc.retryable,
            ) from exc
        except (BridgeTokenError, BridgeIdentityError) as exc:
            raise SelectionQueryError(
                "bridge.auth_failed",
                "The Secure Bridge identity could not be verified.",
                retryable=False,
            ) from exc
        except TimeoutError as exc:
            raise SelectionQueryError(
                "bridge.deadline_exceeded",
                "The Secure Bridge selection query exceeded its deadline.",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise SelectionQueryError(
                "bridge.not_available",
                "The Secure Bridge is not currently available.",
                retryable=True,
            ) from exc
    raise AssertionError("selection retry loop must return or raise")


class SecureBridgeHost:
    """One idempotent Secure Bridge lifecycle owned by the Houdini process."""

    def __init__(
        self,
        *,
        hou_module: object,
        state_dir: Path | str,
        adapter_factory: Callable[[], HoudiniSceneAdapter] = (
            create_houdini_scene_adapter
        ),
        identity_factory: Callable[[], object] = create_bridge_identity,
        queue_factory: Callable[[], MainThreadReadQueue] = MainThreadReadQueue,
        server_factory: Callable[..., BridgeServer] = BridgeServer,
        start_server_factory: Callable[..., object] = asyncio.start_server,
        loop_factory: Callable[[], asyncio.AbstractEventLoop] = (
            _new_background_event_loop
        ),
        start_timeout: float = _START_TIMEOUT_SECONDS,
        join_timeout: float = _JOIN_TIMEOUT_SECONDS,
    ) -> None:
        if type(start_timeout) not in (int, float) or start_timeout <= 0:
            raise ValueError("start_timeout must be positive")
        if type(join_timeout) not in (int, float) or join_timeout <= 0:
            raise ValueError("join_timeout must be positive")
        self._hou = hou_module
        self._state_dir = Path(state_dir)
        self._adapter_factory = adapter_factory
        self._identity_factory = identity_factory
        self._queue_factory = queue_factory
        self._server_factory = server_factory
        self._start_server_factory = start_server_factory
        self._loop_factory = loop_factory
        self._start_timeout = float(start_timeout)
        self._join_timeout = float(join_timeout)

        self._adapter: HoudiniSceneAdapter | None = None
        self._queue: MainThreadReadQueue | None = None
        self._server: BridgeServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._async_stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._port: int | None = None
        self._pump_registered = False
        self._running = False
        self._lock = threading.RLock()
        self._main_thread_id = threading.get_ident()

    @property
    def is_running(self) -> bool:
        with self._lock:
            thread = self._thread
            return (
                self._running
                and self._error is None
                and thread is not None
                and thread.is_alive()
            )

    @property
    def port(self) -> int | None:
        return self._port if self.is_running else None

    @property
    def queue(self) -> MainThreadReadQueue | None:
        return self._queue

    def start(self) -> "SecureBridgeHost":
        """Start once; repeated calls return the same running host."""
        with self._lock:
            if self.is_running:
                return self
            if threading.get_ident() != self._main_thread_id:
                raise SecureBridgeHostError(
                    "The Secure Bridge must be started on Houdini's main thread."
                )
            self._ready.clear()
            self._error = None
            self._port = None
            self._adapter = self._adapter_factory()
            self._queue = self._queue_factory()
            identity = self._identity_factory()
            self._server = self._server_factory(
                adapter=self._adapter,
                identity=identity,
                state_dir=self._state_dir,
                queue=self._queue,
            )
            self._register_pump()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="EEE-SecureBridge",
                daemon=True,
            )
            self._thread.start()

        if not self._ready.wait(self._start_timeout):
            self.stop()
            raise SecureBridgeHostError(
                "The Secure Bridge did not start within the allowed time."
            )
        if self._error is not None:
            error = self._error
            self._finish_main_thread_cleanup()
            thread = self._thread
            if thread is not None:
                thread.join(self._join_timeout)
            raise SecureBridgeHostError(
                "The Secure Bridge could not be started."
            ) from error
        try:
            assert self._adapter is not None
            self._adapter.install_scene_epoch_callbacks()
        except BaseException as exc:
            self.stop()
            raise SecureBridgeHostError(
                "The Houdini scene identity callback could not be installed."
            ) from exc
        with self._lock:
            self._running = True
        return self

    def stop(self) -> None:
        """Stop transport and clean Houdini callbacks; idempotent."""
        with self._lock:
            thread = self._thread
            loop = self._loop
            async_stop = self._async_stop
            queue = self._queue
            adapter = self._adapter
            self._running = False
            self._remove_pump()
            if queue is not None:
                queue.shutdown()
            if adapter is not None:
                adapter.close()
            if loop is not None and async_stop is not None:
                try:
                    loop.call_soon_threadsafe(async_stop.set)
                except RuntimeError:
                    pass
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(self._join_timeout)
        self._finish_main_thread_cleanup()

    def _thread_main(self) -> None:
        loop: asyncio.AbstractEventLoop | None = None
        try:
            loop = self._loop_factory()
            self._loop = loop
            loop.run_until_complete(self._run_server_loop())
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if loop is not None:
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()
            self._loop = None
            self._async_stop = None

    async def _run_server_loop(self) -> None:
        self._async_stop = asyncio.Event()
        await self._serve()

    async def _serve(self) -> None:
        assert self._server is not None
        assert self._async_stop is not None
        listener = None
        try:
            listener = await self._start_server_factory(
                self._server.handle_connection,
                _HOST,
                0,
            )
            self._port = await self._server.serve(listener, host=_HOST)
            self._ready.set()
            await self._async_stop.wait()
        finally:
            # On normal stop, the main thread has already shut down the queue
            # and removed the adapter's HOM callback. BridgeServer.stop() can
            # now close only transport/writers/files; adapter.close() is a no-op.
            await self._server.stop()

    def _register_pump(self) -> None:
        if self._pump_registered:
            return
        self._hou.ui.addEventLoopCallback(self._pump)
        self._pump_registered = True

    def _remove_pump(self) -> None:
        if not self._pump_registered:
            return
        try:
            self._hou.ui.removeEventLoopCallback(self._pump)
        except Exception:
            pass
        self._pump_registered = False

    def _pump(self) -> None:
        """Houdini main-thread idle callback; runs a bounded FIFO batch."""
        if threading.get_ident() != self._main_thread_id:
            return
        if self._error is not None:
            self._finish_main_thread_cleanup()
            return
        queue = self._queue
        if queue is None:
            return
        for _ in range(_PUMP_BUDGET):
            if not queue.pump_one():
                break

    def _finish_main_thread_cleanup(self) -> None:
        if threading.get_ident() == self._main_thread_id:
            self._remove_pump()
            adapter = self._adapter
            if adapter is not None:
                adapter.close()
        with self._lock:
            self._running = False


_host: SecureBridgeHost | None = None
_atexit_registered = False


def start() -> SecureBridgeHost:
    """Start or return the process-global Houdini Secure Bridge host."""
    global _host, _atexit_registered
    if _host is not None and _host.is_running:
        return _host
    import hou

    host = SecureBridgeHost(
        hou_module=hou,
        state_dir=runtime_state_dir(),
    )
    host.start()
    _host = host
    if not _atexit_registered:
        atexit.register(stop)
        _atexit_registered = True
    print(f"[eee] Secure Bridge listening on {_HOST}:{host.port}")
    return host


def stop() -> None:
    """Stop the process-global Secure Bridge host, if any."""
    global _host
    host = _host
    _host = None
    if host is not None:
        host.stop()
        print("[eee] Secure Bridge stopped")


def status() -> dict[str, object]:
    """Return bounded host status without exposing identity credentials."""
    host = _host
    return {
        "running": bool(host is not None and host.is_running),
        "host": _HOST,
        "port": host.port if host is not None else None,
    }


__all__ = [
    "SecureBridgeHost",
    "SecureBridgeHostError",
    "SelectionQueryError",
    "query_selection",
    "run_background_async",
    "start",
    "status",
    "stop",
]
