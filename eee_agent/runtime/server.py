"""Authenticated loopback WebSocket server for the Runtime.

A thin adapter over :class:`RuntimeService`: it performs bearer-token
handshake auth, parses/serializes the wire protocol, routes commands to the
service, and streams committed events to subscribers with bounded per-client
outbound queues. It imports no repository, SQLite, checkpoint, or runner code;
all persistence goes through ``RuntimeService``.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from http import HTTPStatus

from websockets.asyncio.server import serve

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.runtime.auth import RuntimeIdentity, validate_bearer
from eee_agent.runtime.models import thaw_json
from eee_agent.runtime.protocol import (
    DEFERRED_COMMAND_TYPES,
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    encode_envelope,
    error_response,
    event_envelope,
    parse_command,
    success_response,
)

_QUEUE_MAX = 256
_SLOW_CODE = 1008
_SLOW_REASON = "runtime.slow_consumer"
_INCOMPATIBLE_REASON = "protocol.incompatible_version"
_SUBSCRIBE_LIMIT = 1000
_SHUTDOWN = object()

# Exact ChangeSet approval payload shapes (chg_<uuid> / 64 lowercase hex).
_CHANGE_ID_RE = re.compile(r"^chg_[0-9a-f]{32}$")
_CHANGESET_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class _CloseAction:
    """A sender instruction to close the connection after pending sends."""

    __slots__ = ("code", "reason")

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason

_INTERNAL_FAILURE_ERROR = AgentError(
    code="internal.runtime_failure",
    category=ErrorCategory.INTERNAL_INVARIANT,
    message_for_user="The runtime encountered an unexpected error.",
)


def _invalid_envelope() -> AgentException:
    return AgentException(
        AgentError(
            code="protocol.invalid_envelope",
            category=ErrorCategory.PROTOCOL,
            message_for_user="The Runtime command envelope is invalid.",
        )
    )


def _capability_unavailable() -> AgentException:
    return AgentException(
        AgentError(
            code="runtime.capability_unavailable",
            category=ErrorCategory.VALIDATION,
            message_for_user="This Runtime capability is not available.",
        )
    )


# --- payload type checks (bool is not accepted where int is required) ------


def _is_str(v: object) -> bool:
    return type(v) is str


def _is_non_neg_int(v: object) -> bool:
    return type(v) is int and v >= 0


def _is_limit(v: object) -> bool:
    return type(v) is int and 1 <= v <= _SUBSCRIBE_LIMIT


def _is_change_id(v: object) -> bool:
    # bool is a subclass of int, not str, so exact-str rules out bool/numbers.
    return type(v) is str and _CHANGE_ID_RE.fullmatch(v) is not None


def _is_changeset_digest(v: object) -> bool:
    return type(v) is str and _CHANGESET_DIGEST_RE.fullmatch(v) is not None


def _validate(payload: object, schema: dict) -> dict:
    """Validate ``payload`` is a dict with exactly ``schema`` keys/types."""
    if type(payload) is not dict:
        raise _invalid_envelope()
    if set(payload.keys()) != set(schema.keys()):
        raise _invalid_envelope()
    for field, checker in schema.items():
        if not checker(payload[field]):
            raise _invalid_envelope()
    return payload


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_envelope_from_record(record) -> dict:
    return event_envelope(
        event_id=record.event_id,
        session_id=record.session_id,
        run_id=record.run_id,
        seq=record.seq,
        timestamp=record.timestamp.isoformat(),
        event_type=record.event_type,
        payload=thaw_json(record.payload),
        schema_version=record.schema_version,
    )


def _snapshot_to_dict(snap) -> dict:
    return {
        "session": snap.session.to_dict(),
        "runs": [r.to_dict() for r in snap.runs],
        "active_run": snap.active_run.to_dict() if snap.active_run else None,
        "snapshot_seq": snap.snapshot_seq,
        "has_earlier_runs": snap.has_earlier_runs,
        "earliest_included_run_id": snap.earliest_included_run_id,
        "version_report": snap.version_report,
    }


def _replay_to_dict(replay) -> dict:
    return {
        "events": [e.to_dict() for e in replay.events],
        "replay_floor_seq": replay.replay_floor_seq,
        "last_seq": replay.last_seq,
        "snapshot_required": replay.snapshot_required,
    }


def _control_event(event_type: str, session_id: str, payload: dict) -> dict:
    """A non-persisted control event (no event_id / seq)."""
    return {
        "protocol": PROTOCOL,
        "kind": "event",
        "event_id": None,
        "session_id": session_id,
        "run_id": None,
        "seq": None,
        "timestamp": _now_iso(),
        "type": event_type,
        "payload": payload,
        "schema_version": 1,
    }


def _extract_request_id(raw: object) -> str | None:
    """Best-effort request_id extraction for error responses (lenient)."""
    try:
        if type(raw) is str:
            text = raw
        elif type(raw) is bytes:
            text = raw.decode("utf-8")
        else:
            return None
        obj = json.loads(text)
    except Exception:
        return None
    if type(obj) is dict:
        rid = obj.get("request_id")
        if type(rid) is str:
            return rid
    return None


class _Subscription:
    __slots__ = ("session_id", "unsub", "last_delivered", "buffer", "live")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.unsub = None
        self.last_delivered = 0
        self.buffer: list = []  # live EventRecords committed during init
        self.live = False


class _ClientContext:
    __slots__ = (
        "connection", "queue", "subscriptions", "sender_task",
        "slow_consumer", "closing", "closed",
    )

    def __init__(self, connection) -> None:
        self.connection = connection
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self.subscriptions: dict[str, _Subscription] = {}
        self.sender_task: asyncio.Task | None = None
        self.slow_consumer = False
        self.closing = False
        self.closed = False


class RuntimeWebSocketServer:
    """Authenticated loopback WebSocket server over a RuntimeService."""

    def __init__(
        self, service, identity: RuntimeIdentity, *,
        host: str = "127.0.0.1", port: int = 0,
    ) -> None:
        # The first implementation binds exclusively to 127.0.0.1; reject any
        # other host (including localhost, 0.0.0.0, ::1) before creating a
        # socket.
        if host != "127.0.0.1":
            raise ValueError("Runtime server host must be exactly 127.0.0.1")
        self._service = service
        self._identity = identity
        self._requested_port = port
        self._port: int | None = None
        self._serve_cm = None
        self._server = None
        self._clients: set[_ClientContext] = set()

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("server is not running")
        return self._port

    async def __aenter__(self) -> "RuntimeWebSocketServer":
        self._serve_cm = serve(
            self._handle,
            host="127.0.0.1",
            port=self._requested_port,
            process_request=self._authenticate,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_MESSAGE_BYTES,
            max_queue=16,
            server_header=None,
        )
        self._server = await self._serve_cm.__aenter__()
        self._port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._serve_cm is not None:
            await self._serve_cm.__aexit__(exc_type, exc, tb)
        self._serve_cm = None
        self._server = None
        self._port = None

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------

    async def _authenticate(self, connection, request):
        # Read exactly one Authorization header. Multiple values raise and are
        # rejected. Path, headers, and token are never logged.
        try:
            auth = request.headers.get("Authorization")
        except Exception:
            auth = None
        if not validate_bearer(auth, self._identity):
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")
        return None

    # ------------------------------------------------------------------
    # per-connection handler
    # ------------------------------------------------------------------

    async def _handle(self, connection) -> None:
        ctx = _ClientContext(connection)
        self._clients.add(ctx)
        ctx.sender_task = asyncio.create_task(self._sender(ctx))
        try:
            async for raw in connection:
                await self._handle_message(ctx, raw)
                if ctx.closing or ctx.slow_consumer:
                    break
        except Exception:
            pass
        finally:
            await self._cleanup_client(ctx)

    async def _cleanup_client(self, ctx: _ClientContext) -> None:
        ctx.closed = True
        self._clients.discard(ctx)
        for sub in list(ctx.subscriptions.values()):
            if sub.unsub is not None:
                try:
                    sub.unsub()
                except Exception:
                    pass
        ctx.subscriptions.clear()
        task = ctx.sender_task
        if task is None or task.done():
            return
        try:
            ctx.queue.put_nowait(_SHUTDOWN)
        except asyncio.QueueFull:
            pass
        # Let the sender flush any pending close frame / envelopes before
        # cancelling, so a slow-consumer or incompatible-version close (1008)
        # is actually sent rather than degraded to 1000/no frame.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    def _put(self, ctx: _ClientContext, envelope: object) -> None:
        """Enqueue an envelope for the sender. Never blocks; QueueFull marks
        the client a slow consumer (closed by the sender)."""
        try:
            ctx.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            ctx.slow_consumer = True

    async def _sender(self, ctx: _ClientContext) -> None:
        # The single task that calls websocket.send; no concurrent sends.
        try:
            while True:
                item = await ctx.queue.get()
                if item is _SHUTDOWN:
                    return
                if isinstance(item, _CloseAction):
                    try:
                        await ctx.connection.close(item.code, item.reason)
                    except Exception:
                        pass
                    return
                try:
                    await ctx.connection.send(encode_envelope(item))
                except Exception:
                    return
                if ctx.slow_consumer:
                    try:
                        await ctx.connection.close(_SLOW_CODE, _SLOW_REASON)
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # ------------------------------------------------------------------
    # message handling
    # ------------------------------------------------------------------

    async def _handle_message(self, ctx: _ClientContext, raw) -> None:
        try:
            command = parse_command(raw)
        except AgentException as exc:
            rid = _extract_request_id(raw)
            self._put(ctx, error_response(rid, exc.error))
            if exc.error.code == "protocol.incompatible_version":
                # Send the structured error, then close with a policy-error code.
                # Both go through the single sender task.
                ctx.closing = True
                self._put(
                    ctx, _CloseAction(_SLOW_CODE, _INCOMPATIBLE_REASON)
                )
            return
        except Exception:
            self._put(
                ctx,
                error_response(_extract_request_id(raw), _INTERNAL_FAILURE_ERROR),
            )
            return
        try:
            await self._dispatch(ctx, command)
        except AgentException as exc:
            self._put(ctx, error_response(command.request_id, exc.error))
        except Exception:
            self._put(
                ctx,
                error_response(command.request_id, _INTERNAL_FAILURE_ERROR),
            )

    async def _dispatch(self, ctx: _ClientContext, command) -> None:
        ct = command.command_type
        req = command.request_id
        payload = command.payload

        if ct == "runtime.ping":
            _validate(payload, {})
            self._put(ctx, success_response(req, {"pong": True}))
            return

        if ct in DEFERRED_COMMAND_TYPES:
            raise _capability_unavailable()

        if ct == "session.list":
            if set(payload.keys()) not in (set(), {"include_archived"}):
                raise _invalid_envelope()
            include_archived = False
            if "include_archived" in payload:
                if type(payload["include_archived"]) is not bool:
                    raise _invalid_envelope()
                include_archived = payload["include_archived"]
            result = await self._service.list_sessions(include_archived=include_archived)
            self._put(
                ctx,
                success_response(req, {"sessions": [s.to_dict() for s in result]}),
            )
            return

        if ct == "session.create":
            _validate(payload, {"title": _is_str})
            result = await self._service.create_session(payload["title"])
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "session.rename":
            _validate(payload, {"session_id": _is_str, "title": _is_str})
            result = await self._service.rename_session(payload["session_id"], payload["title"])
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "session.archive":
            _validate(payload, {"session_id": _is_str})
            result = await self._service.archive_session(payload["session_id"])
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "session.delete":
            _validate(payload, {"session_id": _is_str})
            sid = payload["session_id"]
            await self._service.delete_session(sid)
            self._put(ctx, success_response(req, {"session_id": sid}))
            self._broadcast_session_deleted(sid)
            return

        if ct == "session.snapshot":
            _validate(payload, {"session_id": _is_str})
            result = await self._service.snapshot(payload["session_id"])
            self._put(ctx, success_response(req, _snapshot_to_dict(result)))
            return

        if ct == "events.replay":
            _validate(
                payload,
                {"session_id": _is_str, "after_seq": _is_non_neg_int, "limit": _is_limit},
            )
            result = await self._service.replay(
                payload["session_id"],
                after_seq=payload["after_seq"],
                limit=payload["limit"],
            )
            self._put(ctx, success_response(req, _replay_to_dict(result)))
            return

        if ct == "session.subscribe":
            _validate(payload, {"session_id": _is_str, "last_seq": _is_non_neg_int})
            await self._do_subscribe(ctx, req, payload["session_id"], payload["last_seq"])
            return

        if ct == "run.start":
            _validate(payload, {"session_id": _is_str, "user_input": _is_str})
            result = await self._service.start_run(payload["session_id"], payload["user_input"])
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "run.stop":
            _validate(payload, {"run_id": _is_str})
            result = await self._service.stop_run(payload["run_id"], force=False)
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "run.force_stop":
            _validate(payload, {"run_id": _is_str})
            result = await self._service.stop_run(payload["run_id"], force=True)
            self._put(ctx, success_response(req, result.to_dict()))
            return

        if ct == "changeset.approve":
            _validate(
                payload,
                {"change_id": _is_change_id, "changeset_digest": _is_changeset_digest},
            )
            result = await self._service.approve_changeset(
                payload["change_id"], payload["changeset_digest"]
            )
            self._put(ctx, success_response(req, result))
            return

        if ct == "changeset.reject":
            _validate(
                payload,
                {"change_id": _is_change_id, "changeset_digest": _is_changeset_digest},
            )
            result = await self._service.reject_changeset(
                payload["change_id"], payload["changeset_digest"]
            )
            self._put(ctx, success_response(req, result))
            return

        # COMMAND_TYPES are all handled above; unreachable for valid commands.
        raise _invalid_envelope()

    # ------------------------------------------------------------------
    # subscription
    # ------------------------------------------------------------------

    async def _do_subscribe(
        self, ctx: _ClientContext, req: str, session_id: str, last_seq: int
    ) -> None:
        # Duplicate subscribe on the same connection: no duplicate events and no
        # re-replay. Report the subscription's actual last_delivered — never the
        # caller's (possibly stale or bogus) last_seq.
        if session_id in ctx.subscriptions:
            existing = ctx.subscriptions[session_id]
            self._put(
                ctx,
                success_response(
                    req,
                    {"session_id": session_id, "last_seq": existing.last_delivered},
                ),
            )
            return

        sub = _Subscription(session_id)
        ctx.subscriptions[session_id] = sub

        def on_event(record) -> None:
            # Called synchronously by the service after the event commits.
            # Never blocks: put_nowait; QueueFull marks slow consumer.
            if record.session_id != session_id:
                return
            if not sub.live:
                # Buffer live events during initialization; deduped after replay.
                sub.buffer.append(record)
                return
            if record.seq > sub.last_delivered:
                self._put(ctx, _event_envelope_from_record(record))
                sub.last_delivered = record.seq

        # Register the callback BEFORE any snapshot/replay so live events
        # committed during initialization are buffered (never lost).
        sub.unsub = self._service.subscribe(on_event)
        init_ok = False
        try:
            boundary = last_seq
            snapshot_to_send = None
            # First replay detects whether a retention gap exists.
            replay = await self._service.replay(
                session_id, after_seq=boundary, limit=_SUBSCRIBE_LIMIT
            )
            if replay.snapshot_required:
                # Retention gap: a snapshot establishes a recovery point, then
                # we re-replay with after_seq=snapshot_seq so only events after
                # the snapshot are delivered. The first replay's events (which
                # overlap the snapshot) are discarded. Re-snapshot while a gap
                # is still reported. The snapshot_seq is only the RECOVERY point
                # used to re-replay — it is NOT the final boundary.
                while replay.snapshot_required:
                    snap = await self._service.snapshot(session_id)
                    snapshot_to_send = snap
                    replay = await self._service.replay(
                        session_id, after_seq=snap.snapshot_seq,
                        limit=_SUBSCRIBE_LIMIT,
                    )
            # The final replay is always gap-free (snapshot_required is False),
            # so its last_seq is the authoritative final boundary. In the gap
            # path this may be GREATER than the snapshot_seq: the re-replay can
            # return newer events, and that advanced boundary is what dedups the
            # overlapping buffered entries against the replayed ones.
            boundary = replay.last_seq
            if ctx.slow_consumer:
                return  # init aborted; finally cleans up
            # Response first, then snapshot (if gap), then replay events.
            self._put(
                ctx,
                success_response(req, {"session_id": session_id, "last_seq": boundary}),
            )
            if snapshot_to_send is not None:
                self._put(
                    ctx,
                    _control_event(
                        "session.snapshot", session_id, _snapshot_to_dict(snapshot_to_send)
                    ),
                )
            for ev in replay.events:
                self._put(ctx, _event_envelope_from_record(ev))
            sub.last_delivered = boundary
            # Flush live events committed during init (deduped by seq > boundary).
            for record in sorted(sub.buffer, key=lambda r: r.seq):
                if record.seq > sub.last_delivered:
                    self._put(ctx, _event_envelope_from_record(record))
                    sub.last_delivered = record.seq
            sub.buffer.clear()
            if ctx.slow_consumer:
                return  # init aborted; finally cleans up
            sub.live = True
            init_ok = True
        finally:
            if not init_ok:
                # Atomic cleanup: a half-initialized subscription is removed and
                # its callback unsubscribed so a later subscribe re-replays.
                ctx.subscriptions.pop(session_id, None)
                if sub.unsub is not None:
                    try:
                        sub.unsub()
                    except Exception:
                        pass
                sub.buffer.clear()

    def _broadcast_session_deleted(self, session_id: str) -> None:
        # Non-persisted control event to current subscribers; remove their
        # subscriptions. No event is inserted into the deleted session.
        for ctx in list(self._clients):
            sub = ctx.subscriptions.pop(session_id, None)
            if sub is None:
                continue
            if sub.unsub is not None:
                try:
                    sub.unsub()
                except Exception:
                    pass
            self._put(
                ctx,
                _control_event("session.deleted", session_id, {"session_id": session_id}),
            )
