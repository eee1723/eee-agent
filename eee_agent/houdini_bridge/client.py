"""Authenticated read-only Bridge client over a framed loopback transport.

The client speaks the length-prefixed (4-byte big-endian) JSON frame protocol on
a ``127.0.0.1``-only connection:

1. ``open()`` establishes the transport and sends the first frame — a ``hello``
   carrying the full Bridge token — then awaits the server ack.
2. Only after a successful hello may ``request()`` send a ``BridgeRequest`` frame
   (``BridgeRequest.to_json()``) and read back a response frame parsed with
   ``parse_response()``.

The client never imports ``hou``/``rpyc``, never returns a HOM proxy, performs
no retry/broadcast/background work, and never places the token in exception
text. A transport can be injected via ``transport_factory`` for testing.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from eee_agent.houdini_bridge.auth import (
    BRIDGE_DISCOVERY_FILENAME,
    BridgeIdentity,
    BridgeIdentityError,
    BridgeTokenError,
    load_bridge_identity,
    read_bridge_discovery,
)
from eee_agent.houdini_bridge.changesets import (
    CHANGESET_V1,
    ApplyRequest,
    ApplyResponse,
    PreflightRequest,
    PreflightResult,
    ReceiptRequest,
    ReceiptResponse,
    parse_apply_response,
    parse_preflight_response,
    parse_receipt_response,
    validate_capabilities,
)
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeRequest,
    SceneQueryResult,
    parse_response,
)
from eee_agent.houdini_bridge.workspaces import (
    WORKSPACE_V1,
    WorkspaceInspectRequest,
    WorkspaceInspectResult,
    parse_workspace_inspect_response,
)
from eee_agent.runtime.models import canonical_json_dumps

_LOOPBACK_HOST = "127.0.0.1"
_MIN_PORT = 1
_MAX_PORT = 65535
_HEADER_LEN = 4


# --------------------------------------------------------------------------
# structured client error
# --------------------------------------------------------------------------


class BridgeClientError(Exception):
    """Structured error raised by the Bridge client.

    Carries the bridge error fields verbatim (code/category/message_for_user/
    retryable/technical_detail_ref). The token is never embedded in the message
    or repr.
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
        retryable: bool,
        technical_detail_ref: str | None = None,
    ) -> None:
        self.code = code
        self.category = category
        self.message_for_user = message_for_user
        self.retryable = retryable
        self.technical_detail_ref = technical_detail_ref
        super().__init__(message_for_user)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "category": self.category,
            "message_for_user": self.message_for_user,
            "retryable": self.retryable,
            "technical_detail_ref": self.technical_detail_ref,
        }


def _client_error(
    code: str, category: str, message: str, *, retryable: bool = False
) -> BridgeClientError:
    return BridgeClientError(
        code=code,
        category=category,
        message_for_user=message,
        retryable=retryable,
    )


# --------------------------------------------------------------------------
# transport abstraction
# --------------------------------------------------------------------------


@runtime_checkable
class BridgeTransport(Protocol):
    """Async byte-stream transport for the framed bridge protocol."""

    async def read_exactly(self, n: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class _StreamReaderTransport:
    """Real asyncio ``StreamReader``/``StreamWriter`` adapter."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def read_exactly(self, n: int) -> bytes:
        return await self._reader.readexactly(n)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except OSError:
            pass


# --------------------------------------------------------------------------
# strict JSON for the hello handshake (response frames use parse_response)
# --------------------------------------------------------------------------


class _DuplicateKeyError(ValueError):
    """Raised by the JSON object_pairs_hook on any duplicate object key."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateKeyError("duplicate object key")
        seen.add(key)
    return dict(pairs)


def _loads_strict_json(data: bytes, *, label: str) -> object:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _client_error(
            "bridge.invalid_request",
            "protocol",
            f"The {label} is not valid UTF-8.",
        ) from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateKeyError, json.JSONDecodeError) as exc:
        raise _client_error(
            "bridge.invalid_request",
            "protocol",
            f"The {label} is not valid JSON.",
        ) from exc


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------


class BridgeClient:
    """Authenticated read-only Bridge client.

    Use as an async context manager or call ``open()``/``close()`` explicitly::

        client = BridgeClient(host="127.0.0.1", port=18811, identity=identity)
        await client.open()
        try:
            result = await client.request(request)
        finally:
            await client.close()
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        identity: BridgeIdentity,
        transport_factory: "BridgeTransportFactory | None" = None,
    ) -> None:
        if type(host) is not str or host != _LOOPBACK_HOST:
            raise ValueError("BridgeClient host must be 127.0.0.1")
        if type(port) is not int or port < _MIN_PORT or port > _MAX_PORT:
            raise ValueError("BridgeClient port must be an integer in 1..65535")
        if not isinstance(identity, BridgeIdentity):
            raise TypeError("BridgeClient identity must be a BridgeIdentity")
        self._host = host
        self._port = port
        self._identity = identity
        self._transport_factory = transport_factory
        self._transport: BridgeTransport | None = None
        self._opened = False
        self._helloed = False
        self._closed = False
        # Capabilities advertised by the server on a successful hello. A legacy
        # ack (no ``capabilities`` key) yields an empty tuple; preflight requires
        # ``changeset.v1`` before any frame is sent.
        self._capabilities: tuple[str, ...] = ()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def capabilities(self) -> tuple[str, ...]:
        """The exact capabilities advertised by the server (empty until hello)."""
        return self._capabilities

    @classmethod
    def from_state_dir(
        cls,
        state_dir: Path | str,
        *,
        transport_factory: "BridgeTransportFactory | None" = None,
    ) -> "BridgeClient":
        """Build a client from the discovery + ``bridge.token`` handoff files.

        Reads the host/port from discovery and constructs the in-memory identity
        from the token file, verifying the fingerprint BEFORE any connection is
        opened. The token is never taken from an env var, CLI arg, Runtime
        token, or SQLite row. Raises :class:`BridgeTokenError` /
        :class:`BridgeIdentityError` if the handoff files are missing,
        malformed, or fail fingerprint verification.
        """
        discovery = read_bridge_discovery(Path(state_dir) / BRIDGE_DISCOVERY_FILENAME)
        host = discovery.get("host")
        port = discovery.get("port")
        if type(host) is not str or host != _LOOPBACK_HOST:
            raise BridgeTokenError("bridge discovery host must be 127.0.0.1")
        if type(port) is not int or port < _MIN_PORT or port > _MAX_PORT:
            raise BridgeTokenError("bridge discovery port is invalid")
        identity = load_bridge_identity(state_dir)
        return cls(
            host=host,
            port=port,
            identity=identity,
            transport_factory=transport_factory,
        )

    # -- lifecycle --------------------------------------------------------

    async def open(self) -> None:
        """Establish the transport and complete the token handshake.

        Raises ``RuntimeError`` if already opened (no re-open/reconnect).
        Raises :class:`BridgeClientError` on a handshake failure, closing the
        transport first.
        """
        if self._opened:
            raise RuntimeError("BridgeClient.open() has already been called")
        self._opened = True
        transport = await self._make_transport()
        self._transport = transport
        try:
            await self._handshake()
        except BaseException:
            await self._close_transport(transport)
            self._transport = None
            raise
        self._helloed = True

    async def close(self) -> None:
        """Close the transport. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._helloed = False
        self._capabilities = ()
        transport = self._transport
        self._transport = None
        if transport is not None:
            await self._close_transport(transport)

    async def __aenter__(self) -> "BridgeClient":
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.close()

    # -- request ----------------------------------------------------------

    async def request(self, request: BridgeRequest) -> SceneQueryResult:
        """Send a read-only request and return the result DTO.

        Raises ``RuntimeError`` if called before a successful hello. Enforces
        the request deadline (closes the transport on expiry). On a server
        bridge error the structured fields are re-raised as
        :class:`BridgeClientError`; the connection is left open for that case.
        """
        if type(request) is not BridgeRequest:
            raise TypeError("request must be a BridgeRequest")
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.request() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        # Parse + validate outside the I/O timeout (pure CPU work).
        try:
            response = parse_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid response envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            # Server-reported error: connection is still valid, do not close.
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        return response.result

    async def preflight(self, request: PreflightRequest) -> PreflightResult:
        """Send a ``changeset.preflight`` request and return the typed facts.

        Requires the advertised ``changeset.v1`` capability. If it is absent the
        client fails closed with ``bridge.capability_unavailable`` and sends no
        frame. On a server bridge error the structured fields are re-raised as
        :class:`BridgeClientError`; the connection is left open for that case.
        """
        if type(request) is not PreflightRequest:
            raise TypeError("request must be a PreflightRequest")
        if CHANGESET_V1 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support changeset preflight.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.preflight() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_preflight_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid preflight envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        return response.result

    async def inspect_workspace(
        self, request: WorkspaceInspectRequest
    ) -> WorkspaceInspectResult:
        """Return bounded live facts for selection or manifest workspace mode."""
        if type(request) is not WorkspaceInspectRequest:
            raise TypeError("request must be a WorkspaceInspectRequest")
        if WORKSPACE_V1 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support workspace inspection.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.inspect_workspace() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_workspace_inspect_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid workspace inspection envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        assert response.result is not None
        return response.result

    async def apply(self, request: ApplyRequest):  # type: ignore[no-untyped-def]
        """Send a ``changeset.apply`` request and return the typed ChangeReceipt.

        Requires the advertised ``changeset.v1`` capability; if it is absent the
        client fails closed with ``bridge.capability_unavailable`` and sends no
        frame. The short transaction runs to completion on the server once it
        starts: a client cancellation/deadline that lands mid-transaction does
        not interrupt the writes, but the result is not delivered here. In that
        case the caller recovers the outcome via :meth:`receipt`. On a server
        bridge error the structured fields are re-raised as
        :class:`BridgeClientError`; the connection is left open for that case.
        """
        from eee_agent.changesets.contracts import ChangeReceipt

        if type(request) is not ApplyRequest:
            raise TypeError("request must be an ApplyRequest")
        if CHANGESET_V1 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support changeset apply.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.apply() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_apply_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid apply envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        result = response.result
        if type(result) is not ChangeReceipt:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge apply response is not a valid receipt.",
            )
        return result

    async def receipt(self, request: ReceiptRequest):  # type: ignore[no-untyped-def]
        """Send a ``changeset.receipt`` request and return the typed ChangeReceipt.

        Requires the advertised ``changeset.v1`` capability; if it is absent the
        client fails closed with ``bridge.capability_unavailable`` and sends no
        frame. A receipt query never mutates the scene. A not-found or digest-
        conflict is surfaced as a :class:`BridgeClientError` (codes
        ``changeset.receipt_unavailable`` / ``changeset.receipt_conflict``).
        """
        from eee_agent.changesets.contracts import ChangeReceipt

        if type(request) is not ReceiptRequest:
            raise TypeError("request must be a ReceiptRequest")
        if CHANGESET_V1 not in self._capabilities:
            raise _client_error(
                "bridge.capability_unavailable",
                "capability",
                "The bridge does not support changeset receipt queries.",
                retryable=False,
            )
        if not self._helloed or self._transport is None:
            raise RuntimeError(
                "BridgeClient.receipt() requires a successful open()/hello"
            )
        response_bytes = await self._exchange(request.to_json(), request.deadline_ms)
        try:
            response = parse_receipt_response(response_bytes)
        except (TypeError, ValueError) as exc:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response is not a valid receipt envelope.",
            ) from exc
        if response.request_id != request.request_id:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge response does not match the request id.",
            )
        if response.error is not None:
            err = response.error
            raise BridgeClientError(
                code=err.code,
                category=err.category,
                message_for_user=err.message_for_user,
                retryable=err.retryable,
                technical_detail_ref=err.technical_detail_ref,
            )
        result = response.result
        if type(result) is not ChangeReceipt:
            await self._abort()
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge receipt response is not a valid receipt.",
            )
        return result

    # -- internals --------------------------------------------------------

    async def _exchange(self, request_json: str, deadline_ms: int) -> bytes:
        """Send a request frame and read the response frame within the deadline."""
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                await self._send_frame(request_json.encode("utf-8"))
                return await self._recv_frame()
        except TimeoutError as exc:
            await self._abort()
            raise _client_error(
                "bridge.deadline_exceeded",
                "deadline",
                "The bridge request exceeded its deadline.",
                retryable=True,
            ) from exc
        except asyncio.CancelledError:
            await self._abort()
            raise
        except BridgeClientError:
            await self._abort()
            raise

    async def _make_transport(self) -> BridgeTransport:
        if self._transport_factory is not None:
            candidate = self._transport_factory()
            if inspect.isawaitable(candidate):
                candidate = await candidate
            return candidate  # type: ignore[return-value]
        reader, writer = await asyncio.open_connection(self._host, self._port)
        return _StreamReaderTransport(reader, writer)

    async def _handshake(self) -> None:
        assert self._transport is not None
        hello = {"protocol": PROTOCOL, "kind": "hello", "token": self._identity.token}
        await self._send_frame(canonical_json_dumps(hello).encode("utf-8"))
        ack_bytes = await self._recv_frame()
        self._parse_ack(ack_bytes)

    def _parse_ack(self, data: bytes) -> None:
        obj = _loads_strict_json(data, label="hello response")
        if type(obj) is not dict:
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The hello response is invalid.",
            )
        if obj.get("protocol") != PROTOCOL:
            raise _client_error(
                "bridge.incompatible_version",
                "protocol",
                "The bridge protocol version is incompatible.",
            )
        if obj.get("kind") != "hello":
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The hello response is invalid.",
            )
        if obj.get("ok") is not True:
            raise _client_error(
                "bridge.unauthorized",
                "permission",
                "Bridge authentication failed.",
            )
        # Capabilities: a legacy ack omits the field (treated as empty). A
        # malformed advertisement (non-list, non-string, duplicate, unsorted, or
        # bad grammar) fails closed — the client must not trust the bridge.
        caps_field = obj.get("capabilities", None)
        if caps_field is None:
            self._capabilities = ()
        else:
            try:
                self._capabilities = validate_capabilities(caps_field)
            except (TypeError, ValueError) as exc:
                raise _client_error(
                    "bridge.incompatible_version",
                    "protocol",
                    "The bridge advertised malformed capabilities.",
                ) from exc

    async def _send_frame(self, data: bytes) -> None:
        assert self._transport is not None
        if len(data) > MAX_MESSAGE_BYTES:
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge frame exceeds the maximum message size.",
            )
        header = len(data).to_bytes(_HEADER_LEN, "big")
        await self._transport.write(header + data)

    async def _recv_frame(self) -> bytes:
        assert self._transport is not None
        try:
            header = await self._transport.read_exactly(_HEADER_LEN)
        except asyncio.IncompleteReadError as exc:
            raise _client_error(
                "bridge.not_available",
                "not_available",
                "The bridge connection is unavailable.",
                retryable=True,
            ) from exc
        except (OSError, ConnectionError) as exc:
            raise _client_error(
                "bridge.not_available",
                "not_available",
                "The bridge connection is unavailable.",
                retryable=True,
            ) from exc
        length = int.from_bytes(header, "big")
        if length <= 0:
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge frame length is invalid.",
            )
        if length > MAX_MESSAGE_BYTES:
            raise _client_error(
                "bridge.invalid_request",
                "protocol",
                "The bridge frame exceeds the maximum message size.",
            )
        try:
            payload = await self._transport.read_exactly(length)
        except asyncio.IncompleteReadError as exc:
            raise _client_error(
                "bridge.not_available",
                "not_available",
                "The bridge connection is unavailable.",
                retryable=True,
            ) from exc
        except (OSError, ConnectionError) as exc:
            raise _client_error(
                "bridge.not_available",
                "not_available",
                "The bridge connection is unavailable.",
                retryable=True,
            ) from exc
        return payload

    async def _abort(self) -> None:
        """Close the transport after a connection-level failure."""
        self._helloed = False
        transport = self._transport
        self._transport = None
        if transport is not None:
            await self._close_transport(transport)

    async def _close_transport(self, transport: BridgeTransport) -> None:
        try:
            await transport.close()
        except Exception:  # noqa: BLE001 — closing must never mask the real error
            pass


# A transport_factory returns a transport (sync) or an awaitable transport.
BridgeTransportFactory = (
    "BridgeTransport | Callable[[], BridgeTransport | Awaitable[BridgeTransport]]"
)
