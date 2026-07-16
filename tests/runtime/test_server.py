"""Task 12: authenticated loopback WebSocket server integration tests.

Uses a real loopback socket (port 0) and the real ``websockets`` client. A
FakeService covers the RuntimeService public interface; the server routes
through it without importing repositories, SQLite, checkpoints, or the runner.
Tests follow the repo convention: each drives an async scenario via
``asyncio.run`` (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.changesets import (
    OwnedNodeRef,
    WorkspaceHealth,
    WorkspaceInspectionSummary,
    WorkspaceLifecycleSummary,
    WorkspaceManifest,
    WorkspaceSummary,
)
from eee_agent.runtime.auth import create_identity
from eee_agent.runtime.events import ReplayResult
from eee_agent.runtime.models import (
    EventRecord,
    RetentionClass,
    RunRecord,
    RunStatus,
    SessionRecord,
    SessionStatus,
)
from eee_agent.runtime.protocol import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    encode_envelope,
)
from eee_agent.runtime.server import RuntimeWebSocketServer

NOW = datetime(2026, 7, 14, 2, 30, 0, tzinfo=timezone.utc)
SID = f"ses_{'a' * 32}"
RID = f"run_{'b' * 32}"
WID = f"ws_{'c' * 32}"
OTHER_WID = f"ws_{'d' * 32}"
WORKSPACE_REVISION = "e" * 64


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def _session(
    sid: str = SID, title: str = "T", status: SessionStatus = SessionStatus.ACTIVE,
    last_seq: int = 0,
) -> SessionRecord:
    return SessionRecord(sid, title, status, NOW, NOW, last_seq, 0)


def _run_rec(
    rid: str = RID, sid: str = SID, status: RunStatus = RunStatus.COMPLETED,
) -> RunRecord:
    return RunRecord(
        rid, sid, status, "in", "done", NOW, NOW, NOW, None, {"model": "fake"}
    )


def _event(
    seq: int, sid: str = SID, rid: str | None = RID, event_type: str = "run.test",
    payload: dict | None = None,
) -> EventRecord:
    return EventRecord(
        f"evt_{seq:032d}", sid, rid, seq, event_type, NOW,
        payload or {"i": seq}, RetentionClass.DURABLE,
    )


def _workspace_summary(*, changed: bool = True) -> WorkspaceLifecycleSummary:
    return WorkspaceLifecycleSummary(
        workspace=WorkspaceSummary(
            workspace_id=WID,
            revision=WORKSPACE_REVISION,
            node_count=1,
            instance_id="hou_instance_1",
            scene_epoch=7,
            active=True,
        ),
        active_workspace_id=WID,
        state_revision=1,
        changed=changed,
    )


def _workspace_inspection() -> WorkspaceInspectionSummary:
    node = OwnedNodeRef(
        node_id="n_root",
        path="/obj/ws",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    manifest = WorkspaceManifest.build(
        workspace_id=WID,
        session_id=SID,
        instance_id="hou_instance_1",
        scene_epoch=7,
        roots=(node,),
        nodes=(node,),
        created_by_run=RID,
        updated_at=NOW,
    )
    summary = WorkspaceSummary(
        workspace_id=WID,
        revision=manifest.revision,
        node_count=1,
        instance_id="hou_instance_1",
        scene_epoch=7,
        active=True,
    )
    return WorkspaceInspectionSummary(
        workspaces=(summary,),
        active_workspace_id=WID,
        target_manifest=manifest,
        status=WorkspaceHealth.HEALTHY,
    )


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._callbacks: set = set()
        self.list_result: list = [_session()]
        self.create_result: SessionRecord = _session(title="new")
        self.rename_result: SessionRecord = _session(title="renamed")
        self.archive_result: SessionRecord = _session(status=SessionStatus.ARCHIVED)
        self.start_result: RunRecord = _run_rec(status=RunStatus.CREATED)
        self.stop_result: RunRecord = _run_rec(status=RunStatus.STOP_REQUESTED)
        self.snapshot_result: Any = None
        self.replay_result: Any = None
        self.replay_side_effect: Any = None
        self.snapshot_side_effect: Any = None
        self.approve_result: dict = {
            "change_id": SID,
            "approval_id": f"apr_{'c' * 32}",
            "changeset_digest": "a" * 64,
            "decision": "Approved",
            "state": "Approved",
            "approved_instance_id": "hou_instance_1",
            "approved_scene_epoch": 1,
        }
        self.reject_result: dict = {
            "change_id": SID,
            "approval_id": f"apr_{'c' * 32}",
            "changeset_digest": "a" * 64,
            "decision": "Rejected",
            "state": "Rejected",
            "approved_instance_id": None,
            "approved_scene_epoch": None,
        }
        self.approve_side_effect: Any = None
        self.reject_side_effect: Any = None
        self.workspace_lifecycle_result = _workspace_summary()
        self.workspace_inspection_result = _workspace_inspection()
        self.workspace_side_effect: Any = None

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))

    async def create_session(self, title: str) -> SessionRecord:
        self._record("create_session", title=title)
        return self.create_result

    async def list_sessions(self, include_archived: bool = False) -> list:
        self._record("list_sessions", include_archived=include_archived)
        return self.list_result

    async def get_session(self, session_id: str) -> SessionRecord:
        self._record("get_session", session_id=session_id)
        return _session(sid=session_id)

    async def rename_session(self, session_id: str, title: str) -> SessionRecord:
        self._record("rename_session", session_id=session_id, title=title)
        return self.rename_result

    async def archive_session(self, session_id: str) -> SessionRecord:
        self._record("archive_session", session_id=session_id)
        return self.archive_result

    async def delete_session(self, session_id: str) -> None:
        self._record("delete_session", session_id=session_id)

    async def start_run(self, session_id: str, user_input: str) -> RunRecord:
        self._record("start_run", session_id=session_id, user_input=user_input)
        return self.start_result

    async def get_run(self, run_id: str) -> RunRecord:
        self._record("get_run", run_id=run_id)
        return _run_rec(rid=run_id)

    async def wait_for_run(self, run_id: str) -> RunRecord:
        self._record("wait_for_run", run_id=run_id)
        return _run_rec(rid=run_id)

    async def stop_run(self, run_id: str, *, force: bool = False) -> RunRecord:
        self._record("stop_run", run_id=run_id, force=force)
        return self.stop_result

    async def approve_changeset(
        self, change_id: str, changeset_digest: str
    ) -> dict:
        self._record(
            "approve_changeset",
            change_id=change_id,
            changeset_digest=changeset_digest,
        )
        if self.approve_side_effect is not None:
            exc = self.approve_side_effect
            self.approve_side_effect = None
            raise exc
        return self.approve_result

    async def reject_changeset(
        self, change_id: str, changeset_digest: str
    ) -> dict:
        self._record(
            "reject_changeset",
            change_id=change_id,
            changeset_digest=changeset_digest,
        )
        if self.reject_side_effect is not None:
            exc = self.reject_side_effect
            self.reject_side_effect = None
            raise exc
        return self.reject_result

    async def create_workspace(
        self, session_id: str, *, expected_scene_epoch: int | None
    ) -> WorkspaceLifecycleSummary:
        self._record(
            "create_workspace",
            session_id=session_id,
            expected_scene_epoch=expected_scene_epoch,
        )
        if self.workspace_side_effect is not None:
            raise self.workspace_side_effect
        return self.workspace_lifecycle_result

    async def bind_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_manifest_revision: str,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        self._record(
            "bind_workspace",
            session_id=session_id,
            workspace_id=workspace_id,
            expected_manifest_revision=expected_manifest_revision,
            expected_scene_epoch=expected_scene_epoch,
        )
        if self.workspace_side_effect is not None:
            raise self.workspace_side_effect
        return self.workspace_lifecycle_result

    async def switch_workspace(
        self,
        session_id: str,
        workspace_id: str,
        *,
        expected_active_workspace_id: str | None,
        expected_scene_epoch: int | None,
    ) -> WorkspaceLifecycleSummary:
        self._record(
            "switch_workspace",
            session_id=session_id,
            workspace_id=workspace_id,
            expected_active_workspace_id=expected_active_workspace_id,
            expected_scene_epoch=expected_scene_epoch,
        )
        if self.workspace_side_effect is not None:
            raise self.workspace_side_effect
        return self.workspace_lifecycle_result

    async def inspect_workspace(
        self,
        session_id: str,
        workspace_id: str | None,
        *,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectionSummary:
        self._record(
            "inspect_workspace",
            session_id=session_id,
            workspace_id=workspace_id,
            expected_scene_epoch=expected_scene_epoch,
        )
        if self.workspace_side_effect is not None:
            raise self.workspace_side_effect
        return self.workspace_inspection_result

    async def replay(self, session_id: str, *, after_seq: int, limit: int):
        self._record(
            "replay", session_id=session_id, after_seq=after_seq, limit=limit
        )
        if self.replay_side_effect is not None:
            exc = self.replay_side_effect
            self.replay_side_effect = None
            raise exc
        rr = self.replay_result
        if callable(rr):
            result = rr(after_seq)
            if inspect.isawaitable(result):
                result = await result
            return result
        return rr

    async def snapshot(self, session_id: str):
        self._record("snapshot", session_id=session_id)
        if self.snapshot_side_effect is not None:
            exc = self.snapshot_side_effect
            self.snapshot_side_effect = None
            raise exc
        snap = self.snapshot_result
        if callable(snap):
            result = snap()
            if inspect.isawaitable(result):
                result = await result
            return result
        return snap

    def subscribe(self, callback) -> Any:
        self._callbacks.add(callback)

        def unsubscribe() -> None:
            self._callbacks.discard(callback)

        return unsubscribe

    def emit(self, record: EventRecord) -> None:
        for cb in list(self._callbacks):
            cb(record)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _cmd(request_id: str, type_: str, payload: dict) -> dict:
    return {
        "protocol": PROTOCOL,
        "kind": "command",
        "request_id": request_id,
        "type": type_,
        "payload": payload,
    }


async def _request(ws, request_id: str, type_: str, payload: dict) -> dict:
    await ws.send(encode_envelope(_cmd(request_id, type_, payload)))
    return json.loads(await ws.recv())


async def _request_raw(ws, raw) -> dict:
    await ws.send(raw)
    return json.loads(await ws.recv())


def _headers(identity, query_token: bool = False) -> dict:
    return {} if query_token else {"Authorization": f"Bearer {identity.token}"}


def _uri(server: RuntimeWebSocketServer) -> str:
    return f"ws://127.0.0.1:{server.port}"


@asynccontextmanager
async def _server(service, identity, *, port=0):
    async with RuntimeWebSocketServer(service, identity, host="127.0.0.1", port=port) as s:
        yield s


@pytest.fixture
def identity():
    return create_identity()


@pytest.fixture
def service() -> FakeService:
    return FakeService()


def _agent_error(code: str) -> dict:
    cats = {
        "protocol.invalid_json": ErrorCategory.PROTOCOL,
        "protocol.invalid_envelope": ErrorCategory.PROTOCOL,
        "protocol.incompatible_version": ErrorCategory.PROTOCOL,
        "protocol.unknown_command": ErrorCategory.PROTOCOL,
        "runtime.capability_unavailable": ErrorCategory.VALIDATION,
    }
    msgs = {
        "protocol.invalid_json": "The Runtime message is not valid JSON.",
        "protocol.invalid_envelope": "The Runtime command envelope is invalid.",
        "protocol.incompatible_version": "The Runtime protocol version is incompatible.",
        "protocol.unknown_command": "The Runtime command is unknown.",
        "runtime.capability_unavailable": "This Runtime capability is not available.",
    }
    return AgentError(code=code, category=cats[code], message_for_user=msgs[code]).to_dict()


def _last(service: FakeService, name: str) -> dict:
    for n, kw in reversed(service.calls):
        if n == name:
            return kw
    raise AssertionError(f"no call to {name}")


# --------------------------------------------------------------------------
# 1. auth and server config
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["localhost", "0.0.0.0", "::1", "192.168.1.5", "127.0.0.2"]
)
def test_non_loopback_host_rejected_before_serve(service, identity, host) -> None:
    with pytest.raises(ValueError):
        RuntimeWebSocketServer(service, identity, host=host, port=0)


def test_only_exact_127001_accepted(service, identity) -> None:
    RuntimeWebSocketServer(service, identity, host="127.0.0.1", port=0)


def test_port_zero_gets_real_ephemeral_port(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            assert isinstance(s.port, int)
            assert s.port > 0
    _run(scenario())


def test_valid_token_upgrades(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "runtime.ping", {})
                assert resp["ok"] is True
    _run(scenario())


def test_missing_token_returns_401(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            with pytest.raises(Exception):
                async with connect(_uri(s), compression=None):
                    pass
    _run(scenario())


def test_malformed_authorization_returns_401(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            with pytest.raises(Exception):
                async with connect(
                    _uri(s), additional_headers={"Authorization": "NotBearer abc"},
                    compression=None,
                ):
                    pass
    _run(scenario())


def test_wrong_token_returns_401(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            with pytest.raises(Exception):
                async with connect(
                    _uri(s), additional_headers={"Authorization": "Bearer wrongtoken"},
                    compression=None,
                ):
                    pass
    _run(scenario())


def test_url_query_token_not_accepted(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            uri = f"{_uri(s)}?token={identity.token}"
            with pytest.raises(Exception):
                async with connect(uri, compression=None):
                    pass
    _run(scenario())


def test_shutdown_closes_socket_and_subscription(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "runtime.ping", {})
        with pytest.raises(Exception):
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None):
                pass
    _run(scenario())


# --------------------------------------------------------------------------
# 2. protocol and responses
# --------------------------------------------------------------------------


def test_ping_returns_exact_pong(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "runtime.ping", {})
                assert resp == {
                    "protocol": PROTOCOL, "kind": "response",
                    "request_id": "r1", "ok": True, "result": {"pong": True},
                }
    _run(scenario())


def test_invalid_json_returns_structured_error(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request_raw(ws, "{not json")
                assert resp["ok"] is False
                assert resp["error"]["code"] == "protocol.invalid_json"
                assert resp["error"] == _agent_error("protocol.invalid_json")
    _run(scenario())


def test_invalid_envelope_returns_structured_error(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                bad = {"protocol": PROTOCOL, "kind": "command", "request_id": "r1", "type": "runtime.ping"}
                resp = await _request_raw(ws, json.dumps(bad))
                assert resp["error"]["code"] == "protocol.invalid_envelope"
    _run(scenario())


def test_duplicate_key_returns_structured_error(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                raw = '{"protocol":"eee.runtime/1","protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{}}'
                resp = await _request_raw(ws, raw)
                assert resp["error"]["code"] == "protocol.invalid_envelope"
    _run(scenario())


def test_unknown_command(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "totally.unknown", {})
                assert resp["error"]["code"] == "protocol.unknown_command"
                assert resp["error"] == _agent_error("protocol.unknown_command")
    _run(scenario())


def test_deferred_command_returns_capability_unavailable(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.fork", {})
                assert resp["error"]["code"] == "runtime.capability_unavailable"
                assert resp["error"] == _agent_error("runtime.capability_unavailable")
    _run(scenario())


def test_binary_utf8_command_handled(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                cmd = _cmd("r1", "session.create", {"title": "中文"})
                resp = await _request_raw(ws, json.dumps(cmd).encode("utf-8"))
                assert resp["ok"] is True
                assert _last(service, "create_session") == {"title": "中文"}
    _run(scenario())


def test_invalid_utf8_returns_invalid_json(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request_raw(ws, b"\xff\xfe\x00bad")
                assert resp["error"]["code"] == "protocol.invalid_json"
    _run(scenario())


def test_one_mib_boundary_accepted(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                raw = json.dumps(_cmd("r1", "runtime.ping", {})).encode("utf-8")
                assert len(raw) <= MAX_MESSAGE_BYTES
                await ws.send(raw)
                resp = json.loads(await ws.recv())
                assert resp["ok"] is True
    _run(scenario())


def test_oversized_message_rejected_or_closed(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                big = json.dumps(_cmd("r1", "runtime.ping", {"x": "a" * MAX_MESSAGE_BYTES}))
                await ws.send(big)
                try:
                    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    assert resp["error"]["code"] == "validation.payload_too_large"
                except (ConnectionClosed, asyncio.TimeoutError):
                    pass
    _run(scenario())


def test_response_never_contains_token_or_traceback(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request_raw(ws, "{not json")
                blob = json.dumps(resp)
                assert identity.token not in blob
                assert "Traceback" not in blob
    _run(scenario())


# --------------------------------------------------------------------------
# 3. command routing
# --------------------------------------------------------------------------


def test_session_list_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.list", {})
                assert resp["ok"] is True
                assert _last(service, "list_sessions") == {"include_archived": False}
                assert resp["result"]["sessions"] == [r.to_dict() for r in service.list_result]
    _run(scenario())


def test_session_list_include_archived_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.list", {"include_archived": True})
                assert _last(service, "list_sessions") == {"include_archived": True}
    _run(scenario())


def test_session_create_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.create", {"title": "Chair"})
                assert resp["ok"] is True
                assert _last(service, "create_session") == {"title": "Chair"}
                assert resp["result"] == service.create_result.to_dict()
    _run(scenario())


def test_session_rename_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.rename", {"session_id": SID, "title": "Stool"})
                assert resp["ok"] is True
                assert _last(service, "rename_session") == {"session_id": SID, "title": "Stool"}
    _run(scenario())


def test_session_archive_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.archive", {"session_id": SID})
                assert resp["ok"] is True
                assert _last(service, "archive_session") == {"session_id": SID}
    _run(scenario())


def test_session_snapshot_routing(service, identity) -> None:
    from eee_agent.runtime.service import SessionSnapshot
    service.snapshot_result = SessionSnapshot(
        session=_session(last_seq=5), runs=(_run_rec(),), active_run=None,
        snapshot_seq=5, has_earlier_runs=False, earliest_included_run_id=None,
        version_report={"eee_agent": "x"},
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.snapshot", {"session_id": SID})
                assert resp["ok"] is True
                assert _last(service, "snapshot") == {"session_id": SID}
                assert resp["result"]["snapshot_seq"] == 5
                assert resp["result"]["session"] == _session(last_seq=5).to_dict()
    _run(scenario())


def test_events_replay_routing(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(1), _event(2)), replay_floor_seq=0, last_seq=2,
        snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "events.replay", {"session_id": SID, "after_seq": 0, "limit": 100})
                assert resp["ok"] is True
                assert _last(service, "replay") == {"session_id": SID, "after_seq": 0, "limit": 100}
                assert [e["event_type"] for e in resp["result"]["events"]] == ["run.test", "run.test"]
                assert resp["result"]["last_seq"] == 2
    _run(scenario())


def test_run_start_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "run.start", {"session_id": SID, "user_input": "inspect"})
                assert resp["ok"] is True
                assert _last(service, "start_run") == {"session_id": SID, "user_input": "inspect"}
                assert resp["result"] == service.start_result.to_dict()
    _run(scenario())


def test_run_stop_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "run.stop", {"run_id": RID})
                assert _last(service, "stop_run") == {"run_id": RID, "force": False}
    _run(scenario())


def test_run_force_stop_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "run.force_stop", {"run_id": RID})
                assert _last(service, "stop_run") == {"run_id": RID, "force": True}
    _run(scenario())


_CHG = f"chg_{'d' * 32}"
_DIGEST = "a" * 64


def _approve_payload(**overrides) -> dict:
    payload = {"change_id": _CHG, "changeset_digest": _DIGEST}
    payload.update(overrides)
    return payload


def test_changeset_approve_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "changeset.approve", _approve_payload())
                assert resp["ok"] is True
                assert _last(service, "approve_changeset") == {
                    "change_id": _CHG, "changeset_digest": _DIGEST,
                }
                assert resp["result"] == service.approve_result
    _run(scenario())


def test_changeset_reject_routing(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "changeset.reject", _approve_payload())
                assert resp["ok"] is True
                assert _last(service, "reject_changeset") == {
                    "change_id": _CHG, "changeset_digest": _DIGEST,
                }
                assert resp["result"] == service.reject_result
    _run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"change_id": _CHG},
        {"changeset_digest": _DIGEST},
        {"change_id": _CHG, "changeset_digest": _DIGEST, "operations": []},
        {"change_id": _CHG, "changeset_digest": _DIGEST, "manifest": {}},
        {"change_id": f"run_{'d' * 32}", "changeset_digest": _DIGEST},
        {"change_id": 123, "changeset_digest": _DIGEST},
        {"change_id": True, "changeset_digest": _DIGEST},
        {"change_id": _CHG, "changeset_digest": 123},
        {"change_id": _CHG, "changeset_digest": "X" * 64},
        {"change_id": _CHG, "changeset_digest": "a" * 63},
        {"change_id": "chg_short", "changeset_digest": _DIGEST},
    ],
    ids=[
        "missing-digest", "missing-id", "extra-operations", "extra-manifest",
        "wrong-id-kind", "id-non-string", "id-bool", "digest-non-string",
        "digest-non-hex", "digest-short", "id-bad",
    ],
)
def test_changeset_approve_rejects_invalid_payload(
    service, identity, payload
) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "changeset.approve", payload)
                assert resp["ok"] is False
                assert resp["error"]["code"] == "protocol.invalid_envelope"
                assert not any(n == "approve_changeset" for n, _ in service.calls)
    _run(scenario())


def test_changeset_approve_maps_service_error(service, identity) -> None:
    from eee_agent.core import AgentError, AgentException, ErrorCategory

    service.approve_side_effect = AgentException(
        AgentError(
            code="approval.expired",
            category=ErrorCategory.VALIDATION,
            message_for_user="The approval has expired.",
        )
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "changeset.approve", _approve_payload())
                assert resp["ok"] is False
                assert resp["error"]["code"] == "approval.expired"
                assert "Traceback" not in json.dumps(resp)
    _run(scenario())


def test_changeset_apply_remains_rejected(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(
                    ws, "r1", "changeset.apply", _approve_payload(operations=[])
                )
                assert resp["ok"] is False
                assert resp["error"]["code"] == "protocol.unknown_command"
    _run(scenario())


@pytest.mark.parametrize(
    ("command_type", "payload", "method", "expected_call", "expected_result"),
    [
        (
            "workspace.create",
            {"session_id": SID, "expected_scene_epoch": None},
            "create_workspace",
            {"session_id": SID, "expected_scene_epoch": None},
            lambda service: service.workspace_lifecycle_result.to_dict(),
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": WORKSPACE_REVISION,
                "expected_scene_epoch": 7,
            },
            "bind_workspace",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": WORKSPACE_REVISION,
                "expected_scene_epoch": 7,
            },
            lambda service: service.workspace_lifecycle_result.to_dict(),
        ),
        (
            "workspace.switch",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_active_workspace_id": OTHER_WID,
                "expected_scene_epoch": None,
            },
            "switch_workspace",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_active_workspace_id": OTHER_WID,
                "expected_scene_epoch": None,
            },
            lambda service: service.workspace_lifecycle_result.to_dict(),
        ),
        (
            "workspace.inspect",
            {
                "session_id": SID,
                "workspace_id": None,
                "expected_scene_epoch": 1,
            },
            "inspect_workspace",
            {
                "session_id": SID,
                "workspace_id": None,
                "expected_scene_epoch": 1,
            },
            lambda service: service.workspace_inspection_result.to_dict(),
        ),
    ],
)
def test_workspace_commands_route_exact_payload_and_serialize_summary(
    service,
    identity,
    command_type,
    payload,
    method,
    expected_call,
    expected_result,
) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(
                _uri(s),
                additional_headers=_headers(identity),
                compression=None,
            ) as ws:
                resp = await _request(ws, "r1", command_type, payload)
                assert resp["ok"] is True
                assert resp["result"] == expected_result(service)
                assert _last(service, method) == expected_call

    _run(scenario())


@pytest.mark.parametrize(
    ("command_type", "payload"),
    [
        ("workspace.create", {"session_id": SID}),
        (
            "workspace.create",
            {
                "session_id": SID,
                "expected_scene_epoch": None,
                "manifest": {},
            },
        ),
        (
            "workspace.create",
            {"session_id": f"run_{'a' * 32}", "expected_scene_epoch": None},
        ),
        (
            "workspace.create",
            {"session_id": SID.upper(), "expected_scene_epoch": None},
        ),
        (
            "workspace.create",
            {"session_id": SID, "expected_scene_epoch": True},
        ),
        (
            "workspace.create",
            {"session_id": SID, "expected_scene_epoch": 0},
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": WORKSPACE_REVISION,
            },
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": f"ses_{'c' * 32}",
                "expected_manifest_revision": WORKSPACE_REVISION,
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": "E" * 64,
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": "e" * 63,
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.bind",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_manifest_revision": WORKSPACE_REVISION,
                "expected_scene_epoch": None,
                "nodes": [],
            },
        ),
        (
            "workspace.switch",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_active_workspace_id": 1,
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.switch",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_active_workspace_id": f"run_{'d' * 32}",
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.switch",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_active_workspace_id": None,
                "expected_scene_epoch": -1,
            },
        ),
        (
            "workspace.inspect",
            {
                "session_id": SID,
                "workspace_id": True,
                "expected_scene_epoch": None,
            },
        ),
        (
            "workspace.inspect",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_scene_epoch": None,
                "paths": ["/obj/ws"],
            },
        ),
        (
            "workspace.inspect",
            {
                "session_id": SID,
                "workspace_id": WID,
                "expected_scene_epoch": None,
                "metadata": {"role": "root", "capability": "modeling"},
            },
        ),
    ],
    ids=[
        "create-missing-epoch",
        "create-manifest-upload",
        "create-wrong-session-kind",
        "create-uppercase-session",
        "create-bool-epoch",
        "create-zero-epoch",
        "bind-missing-epoch",
        "bind-wrong-workspace-kind",
        "bind-uppercase-digest",
        "bind-short-digest",
        "bind-node-upload",
        "switch-active-non-string",
        "switch-active-wrong-kind",
        "switch-negative-epoch",
        "inspect-workspace-bool",
        "inspect-path-upload",
        "inspect-metadata-upload",
    ],
)
def test_workspace_commands_reject_non_exact_payload_before_service(
    service, identity, command_type, payload
) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(
                _uri(s),
                additional_headers=_headers(identity),
                compression=None,
            ) as ws:
                resp = await _request(ws, "r1", command_type, payload)
                assert resp["ok"] is False
                assert resp["error"]["code"] == "protocol.invalid_envelope"

    _run(scenario())
    workspace_methods = {
        "create_workspace",
        "bind_workspace",
        "switch_workspace",
        "inspect_workspace",
    }
    assert not any(name in workspace_methods for name, _ in service.calls)


def test_workspace_command_maps_structured_service_error(service, identity) -> None:
    service.workspace_side_effect = AgentException(
        AgentError(
            code="workspace.identity_conflict",
            category=ErrorCategory.VALIDATION,
            message_for_user="The live workspace identity conflicts.",
        )
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(
                _uri(s),
                additional_headers=_headers(identity),
                compression=None,
            ) as ws:
                resp = await _request(
                    ws,
                    "r1",
                    "workspace.create",
                    {"session_id": SID, "expected_scene_epoch": None},
                )
                assert resp["ok"] is False
                assert resp["error"]["code"] == "workspace.identity_conflict"
                assert "Traceback" not in json.dumps(resp)

    _run(scenario())


@pytest.mark.parametrize(
    "cmd_type, payload",
    [
        ("session.create", {}),
        ("session.create", {"title": 1}),
        ("session.create", {"title": "x", "extra": 1}),
        ("session.rename", {"session_id": SID}),
        ("session.rename", {"session_id": SID, "title": "x", "extra": 1}),
        ("session.archive", {}),
        ("session.archive", {"session_id": SID, "extra": 1}),
        ("session.delete", {}),
        ("session.snapshot", {}),
        ("events.replay", {"session_id": SID, "after_seq": 0}),
        ("events.replay", {"session_id": SID, "after_seq": 0, "limit": 0}),
        ("events.replay", {"session_id": SID, "after_seq": 0, "limit": 1001}),
        ("events.replay", {"session_id": SID, "after_seq": -1, "limit": 10}),
        ("run.start", {"session_id": SID}),
        ("run.start", {"session_id": SID, "user_input": 1}),
        ("run.stop", {}),
        ("run.force_stop", {"run_id": RID, "extra": 1}),
    ],
)
def test_invalid_payload_returns_invalid_envelope_without_service(
    service, identity, cmd_type, payload
) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", cmd_type, payload)
                assert resp["error"]["code"] == "protocol.invalid_envelope"
    _run(scenario())
    mutating = {"create_session", "rename_session", "archive_session",
                "delete_session", "start_run", "stop_run", "snapshot", "replay"}
    assert not any(n in mutating for n, _ in service.calls)


# --------------------------------------------------------------------------
# session.delete control event
# --------------------------------------------------------------------------


def test_session_delete_sends_control_event_and_unsubscribes(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=3, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 3})
                resp = await _request(ws, "r2", "session.delete", {"session_id": SID})
                assert resp["ok"] is True
                ctrl = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert ctrl["type"] == "session.deleted"
                assert ctrl["session_id"] == SID
                assert ctrl["run_id"] is None
                service.emit(_event(4))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
        assert not service._callbacks
    _run(scenario())


# --------------------------------------------------------------------------
# 4. subscription consistency
# --------------------------------------------------------------------------


def test_subscribe_no_gap_replay_then_live(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(4), _event(5)), replay_floor_seq=0, last_seq=5,
        snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 3})))
                resp = json.loads(await ws.recv())
                assert resp["ok"] is True
                e4 = json.loads(await ws.recv())
                e5 = json.loads(await ws.recv())
                assert e4["seq"] == 4
                assert e5["seq"] == 5
                service.emit(_event(6))
                e6 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e6["seq"] == 6
    _run(scenario())


def test_gap_subscribe_sends_snapshot_then_only_events_after_boundary(
    service, identity
) -> None:
    # Retention gap: first replay reports snapshot_required=True with event(6);
    # snapshot.snapshot_seq=6. event(6) is covered by the snapshot and must NOT
    # be re-sent. The server must re-replay with after_seq=N and send only
    # seq>N events, then buffered live events.
    from eee_agent.runtime.service import SessionSnapshot
    service.snapshot_result = SessionSnapshot(
        session=_session(last_seq=6), runs=(), active_run=None,
        snapshot_seq=6, has_earlier_runs=False, earliest_included_run_id=None,
        version_report={"eee_agent": "x"},
    )

    def replay_for(after_seq: int):
        if after_seq <= 1:
            return ReplayResult(
                events=(_event(6),), replay_floor_seq=5, last_seq=6,
                snapshot_required=True,
            )
        # second replay with after_seq=6 (the snapshot boundary)
        return ReplayResult(
            events=(), replay_floor_seq=5, last_seq=6, snapshot_required=False,
        )

    service.replay_result = replay_for

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 1})))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resp["ok"] is True
                assert resp["result"]["last_seq"] == 6
                snap = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert snap["type"] == "session.snapshot"
                assert snap["seq"] is None
                assert snap["payload"]["snapshot_seq"] == 6
                # event(6) must NOT be delivered (it is in the snapshot).
                service.emit(_event(7))
                e7 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e7["seq"] == 7
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
                # The second replay must have been called with after_seq=6.
                replay_calls = [c["after_seq"] for n, c in service.calls if n == "replay"]
                assert replay_calls == [1, 6]
    _run(scenario())


def test_gap_subscribe_concurrent_event_sent_once(service, identity) -> None:
    # An event committed during the snapshot query (N+1) must be buffered and
    # delivered exactly once, not lost or duplicated.
    from eee_agent.runtime.service import SessionSnapshot
    service.snapshot_result = SessionSnapshot(
        session=_session(last_seq=6), runs=(), active_run=None,
        snapshot_seq=6, has_earlier_runs=False, earliest_included_run_id=None,
        version_report={"eee_agent": "x"},
    )

    def replay_for(after_seq: int):
        if after_seq <= 1:
            return ReplayResult(
                events=(_event(6),), replay_floor_seq=5, last_seq=6,
                snapshot_required=True,
            )
        return ReplayResult(
            events=(), replay_floor_seq=5, last_seq=6, snapshot_required=False,
        )

    service.replay_result = replay_for
    original_snapshot = service.snapshot

    async def snapshot_with_emit(sid):
        result = await original_snapshot(sid)
        service.emit(_event(7))  # commit N+1 during snapshot query
        return result

    service.snapshot = snapshot_with_emit

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 1})))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resp["result"]["last_seq"] == 6
                await asyncio.wait_for(ws.recv(), timeout=2)  # snapshot
                e7 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e7["seq"] == 7
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
    _run(scenario())


def test_gap_subscribe_advances_final_replay_boundary_and_deduplicates_overlap(
    service, identity
) -> None:
    # Retention gap where the second replay (after_seq=snapshot_seq) returns a
    # NEWER event than the snapshot boundary: first replay reports a gap with
    # last_seq=6; snapshot_seq=6; second replay returns events=(seq7,) with
    # last_seq=7. The init buffer concurrently holds both seq7 (overlaps the
    # second replay) and seq8 (buffer-only). The final boundary must be the
    # second replay's last_seq (7) — NOT the snapshot boundary (6) — so seq7 is
    # delivered exactly once and seq8 is not lost.
    from eee_agent.runtime.service import SessionSnapshot
    service.snapshot_result = SessionSnapshot(
        session=_session(last_seq=6), runs=(), active_run=None,
        snapshot_seq=6, has_earlier_runs=False, earliest_included_run_id=None,
        version_report={"eee_agent": "x"},
    )

    def replay_for(after_seq: int):
        if after_seq <= 1:
            return ReplayResult(
                events=(_event(6),), replay_floor_seq=5, last_seq=6,
                snapshot_required=True,
            )
        # second replay with after_seq=6 (the snapshot boundary)
        return ReplayResult(
            events=(_event(7),), replay_floor_seq=6, last_seq=7,
            snapshot_required=False,
        )

    service.replay_result = replay_for
    original_snapshot = service.snapshot

    async def snapshot_with_emit(sid):
        result = await original_snapshot(sid)
        # Buffer both seq7 (overlaps the second replay) and seq8 (buffer-only)
        # during init; seq7 must be deduped against the replay event, seq8 must
        # still be delivered.
        service.emit(_event(7))
        service.emit(_event(8))
        return result

    service.snapshot = snapshot_with_emit

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 1})))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resp["ok"] is True
                # final boundary is the second replay's last_seq (7), NOT the
                # snapshot boundary (6).
                assert resp["result"]["last_seq"] == 7
                snap = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert snap["type"] == "session.snapshot"
                assert snap["seq"] is None
                assert snap["payload"]["snapshot_seq"] == 6
                e7 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e7["seq"] == 7
                e8 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e8["seq"] == 8  # seq8 from the init buffer, not lost
                # seq7 appears exactly once — nothing else follows seq8.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
        # the two replays are after_seq=1 (gap detection) then after_seq=6.
        replay_calls = [c["after_seq"] for n, c in service.calls if n == "replay"]
        assert replay_calls == [1, 6]
    _run(scenario())


def test_subscribe_replay_agent_exception_cleans_up(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )
    service.replay_side_effect = AgentException(AgentError(
        code="runtime.session_not_found", category=ErrorCategory.VALIDATION,
        message_for_user="The Runtime session does not exist.",
    ))

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                assert resp["error"]["code"] == "runtime.session_not_found"
                assert resp["error"]["category"] == "validation"
                # Same-connection re-subscribe must re-replay (not a duplicate no-op):
                # the half-initialized subscription was removed.
                resp = await _request(ws, "r2", "session.subscribe", {"session_id": SID, "last_seq": 0})
                assert resp["ok"] is True
    _run(scenario())
    replay_calls = [n for n, _ in service.calls if n == "replay"]
    assert len(replay_calls) == 2  # failed + re-subscribe


def test_subscribe_replay_internal_exception_cleans_up(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )
    service.replay_side_effect = RuntimeError("boom")

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                assert resp["error"]["code"] == "internal.runtime_failure"
                assert "boom" not in json.dumps(resp)
                resp = await _request(ws, "r2", "session.subscribe", {"session_id": SID, "last_seq": 0})
                assert resp["ok"] is True
    _run(scenario())
    assert len([n for n, _ in service.calls if n == "replay"]) == 2


def test_subscribe_snapshot_exception_cleans_up(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(6),), replay_floor_seq=5, last_seq=6, snapshot_required=True,
    )
    service.snapshot_side_effect = RuntimeError("snap boom")

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 1})
                assert resp["error"]["code"] == "internal.runtime_failure"
                assert "snap boom" not in json.dumps(resp)
                # Re-subscribe (no gap this time) must work.
                service.snapshot_side_effect = None
                service.replay_result = ReplayResult(
                    events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
                )
                resp = await _request(ws, "r2", "session.subscribe", {"session_id": SID, "last_seq": 0})
                assert resp["ok"] is True
    _run(scenario())
    assert len([n for n, _ in service.calls if n == "snapshot"]) == 1
    assert len([n for n, _ in service.calls if n == "replay"]) == 2


def test_subscribe_disconnect_during_init_cleans_up(service, identity) -> None:
    async def slow_replay(after_seq: int):
        # Slow (but bounded) replay so the client can disconnect while the
        # handler is still initializing. Bounded so the server can always close.
        await asyncio.sleep(0.3)
        return ReplayResult(events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False)

    service.replay_result = slow_replay

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 0})))
                # Wait until the handler has registered the callback (in init).
                for _ in range(25):
                    if service._callbacks:
                        break
                    await asyncio.sleep(0.02)
                assert service._callbacks
            # Client disconnects during/after init.
            # Wait for the handler to finish the slow replay and clean up.
            for _ in range(40):
                if not service._callbacks:
                    break
                await asyncio.sleep(0.02)
        assert not service._callbacks
    _run(scenario())


def test_incompatible_protocol_responds_then_closes_1008(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                raw = json.dumps({
                    "protocol": "eee.runtime/2", "kind": "command",
                    "request_id": "r1", "type": "runtime.ping", "payload": {},
                })
                await ws.send(raw)
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resp["error"]["code"] == "protocol.incompatible_version"
                blob = json.dumps(resp)
                assert identity.token not in blob
                assert "eee.runtime/2" not in blob
                with pytest.raises(ConnectionClosed) as exc:
                    await asyncio.wait_for(ws.recv(), timeout=2)
                assert exc.value.rcvd.code == 1008
                reason = exc.value.rcvd.reason or ""
                assert identity.token not in reason
                # cannot ping after close
                try:
                    await ws.send(json.dumps(_cmd("r2", "runtime.ping", {})))
                    await asyncio.wait_for(ws.recv(), timeout=1)
                    raise AssertionError("should not be able to ping after close")
                except (ConnectionClosed, asyncio.TimeoutError):
                    pass
    _run(scenario())


def test_initial_replay_queuefull_closes_1008(service, identity) -> None:
    events = tuple(_event(i) for i in range(1, 300))
    service.replay_result = ReplayResult(
        events=events, replay_floor_seq=0, last_seq=299, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 0})))
                with pytest.raises(ConnectionClosed) as exc:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=3)
                assert exc.value.rcvd.code == 1008
                assert "runtime.slow_consumer" in (exc.value.rcvd.reason or "")
        assert not service._callbacks
    _run(scenario())
    assert not any(n == "stop_run" for n, _ in service.calls)


def test_subscribe_live_events_buffered_during_init(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(4),), replay_floor_seq=0, last_seq=4, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 3})))
                await asyncio.sleep(0.05)
                service.emit(_event(5))
                seen = []
                for _ in range(3):
                    seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
                seqs = [m.get("seq") for m in seen if m.get("kind") == "event"]
                assert seqs == [4, 5]
    _run(scenario())


def test_multiple_sessions_per_connection(service, identity) -> None:
    sid2 = f"ses_{'c' * 32}"
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                await _request(ws, "r2", "session.subscribe", {"session_id": sid2, "last_seq": 0})
                service.emit(_event(1, sid=SID))
                service.emit(_event(1, sid=sid2))
                evs = []
                for _ in range(2):
                    evs.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=2)))
                assert sorted(e["session_id"] for e in evs) == sorted([SID, sid2])
    _run(scenario())


def test_duplicate_subscribe_no_duplicate_events(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(1),), replay_floor_seq=0, last_seq=1, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                await asyncio.wait_for(ws.recv(), timeout=2)  # event 1
                await _request(ws, "r2", "session.subscribe", {"session_id": SID, "last_seq": 1})
                service.emit(_event(2))
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert ev["seq"] == 2
    _run(scenario())


def test_duplicate_subscribe_reports_actual_last_delivered(
    service, identity
) -> None:
    # A duplicate subscribe on the same connection must report the existing
    # subscription's actual last_delivered — never the caller's (possibly bogus)
    # last_seq — and must neither re-replay, register a second callback, nor
    # resend any events.
    service.replay_result = ReplayResult(
        events=(_event(1),), replay_floor_seq=0, last_seq=1, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await ws.send(encode_envelope(_cmd("r1", "session.subscribe", {"session_id": SID, "last_seq": 0})))
                resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert resp["ok"] is True
                e1 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e1["seq"] == 1  # replay event advances last_delivered to 1
                service.emit(_event(2))
                e2 = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert e2["seq"] == 2  # live event advances last_delivered to 2
                # duplicate subscribe carries a bogus last_seq; the response must
                # report the subscription's actual last_delivered (2), never the
                # caller's value (999).
                resp2 = await _request(ws, "r3", "session.subscribe", {"session_id": SID, "last_seq": 999})
                assert resp2["ok"] is True
                assert resp2["result"]["last_seq"] == 2
                # the duplicate must not resend any events
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=0.3)
                # checked while connected: cleanup discards callbacks on close
                assert len([n for n, _ in service.calls if n == "replay"]) == 1
                assert len(service._callbacks) == 1
    _run(scenario())


def test_reconnect_no_loss_no_duplicate(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(_event(1), _event(2), _event(3)), replay_floor_seq=0, last_seq=3,
        snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                seqs = []
                for _ in range(3):
                    seqs.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=2))["seq"])
                assert seqs == [1, 2, 3]
            service.replay_result = ReplayResult(
                events=(), replay_floor_seq=0, last_seq=3, snapshot_required=False,
            )
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r2", "session.subscribe", {"session_id": SID, "last_seq": 3})
                service.emit(_event(4))
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert ev["seq"] == 4
    _run(scenario())


def test_last_seq_rejects_bool(service, identity) -> None:
    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                resp = await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": True})
                assert resp["error"]["code"] == "protocol.invalid_envelope"
    _run(scenario())


def test_disconnect_does_not_cancel_run(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
    _run(scenario())
    assert not any(n == "stop_run" for n, _ in service.calls)


# --------------------------------------------------------------------------
# 5. outbound / backpressure
# --------------------------------------------------------------------------


def test_slow_consumer_closed_with_1008(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as ws:
                await _request(ws, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                # Emit > 256 events synchronously so the outbound queue fills
                # before the sender drains (deterministic, no OS-buffer reliance).
                for i in range(1, 300):
                    service.emit(_event(i))
                with pytest.raises(ConnectionClosed) as exc_info:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=3)
                assert exc_info.value.rcvd.code == 1008
                assert "runtime.slow_consumer" in (exc_info.value.rcvd.reason or "")
    _run(scenario())
    assert not any(n == "stop_run" for n, _ in service.calls)


def test_slow_consumer_does_not_affect_other_clients(service, identity) -> None:
    service.replay_result = ReplayResult(
        events=(), replay_floor_seq=0, last_seq=0, snapshot_required=False,
    )
    sid2 = f"ses_{'d' * 32}"

    async def scenario():
        async with _server(service, identity) as s:
            async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as slow:
                async with connect(_uri(s), additional_headers=_headers(identity), compression=None) as fast:
                    await _request(slow, "r1", "session.subscribe", {"session_id": SID, "last_seq": 0})
                    await _request(fast, "r2", "session.subscribe", {"session_id": sid2, "last_seq": 0})
                    for i in range(1, 300):
                        service.emit(_event(i, sid=SID))
                    with pytest.raises(ConnectionClosed):
                        while True:
                            await asyncio.wait_for(slow.recv(), timeout=3)
                    service.emit(_event(1, sid=sid2))
                    ev = json.loads(await asyncio.wait_for(fast.recv(), timeout=3))
                    assert ev["session_id"] == sid2
    _run(scenario())
