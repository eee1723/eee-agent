"""Task 15-B: authenticated read-only Bridge client tests.

Drives :class:`BridgeClient` entirely through an injectable fake transport — no
real socket, no ``hou``, no ``rpyc``. Async scenarios run via ``asyncio.run``
(this suite deliberately avoids pytest-asyncio, matching the rest of the Runtime
tests).
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Awaitable, Callable

import pytest

from eee_agent.houdini_bridge.auth import BridgeIdentity, create_bridge_identity
from eee_agent.houdini_bridge.client import BridgeClient, BridgeClientError
from eee_agent.houdini_bridge.contracts import (
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    BridgeError,
    BridgeRequest,
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)

_PROTO = PROTOCOL


# --------------------------------------------------------------------------
# test helpers
# --------------------------------------------------------------------------


def async_test(coro: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    @functools.wraps(coro)
    def wrapper() -> None:
        asyncio.run(coro())

    return wrapper


def _frame(payload: bytes | str) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return len(payload).to_bytes(4, "big") + payload


def _dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ack_frame(ok: bool = True, caps: list[str] | None = None) -> bytes:
    ack: dict[str, object] = {"protocol": _PROTO, "kind": "hello", "ok": ok}
    if caps is not None:
        ack["capabilities"] = caps
    return _frame(_dumps(ack))


def _sample_result() -> SceneQueryResult:
    return SceneQueryResult(
        binding=SceneBinding(
            instance_id="hou_instance_1",
            scene_epoch=1,
            hip_path=None,
            observed_revision="sha256:abc",
        ),
        selected_nodes=(
            SelectedNode(
                path="/obj/geo1",
                node_type="geo",
                parent_path="/obj",
                display_name="geo1",
                is_locked=False,
                geometry_stats={"points": 8, "primitives": 6},
            ),
        ),
        nodes=(),
    )


def _success_frame(request_id: str, result: SceneQueryResult | None = None) -> bytes:
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": True,
        "result": (result or _sample_result()).to_dict(),
    }
    return _frame(_dumps(response))


def _error_frame(request_id: str, error: BridgeError) -> bytes:
    response = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": request_id,
        "ok": False,
        "error": error.to_dict(),
    }
    return _frame(_dumps(response))


def _parse_frames(data: bytes) -> list[bytes]:
    frames: list[bytes] = []
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i : i + 4], "big")
        start = i + 4
        frames.append(bytes(data[start : start + length]))
        i = start + length
    return frames


def _make_request(request_id: str = "req_test", deadline_ms: int = 5000) -> BridgeRequest:
    return BridgeRequest.from_dict(
        {
            "protocol": _PROTO,
            "kind": "request",
            "request_id": request_id,
            "operation": "scene.query",
            "deadline_ms": deadline_ms,
            "scene_epoch": 1,
            "payload": {
                "include_selection": True,
                "node_paths": [],
                "include_geometry_stats": True,
            },
        }
    )


class FakeTransport:
    """In-memory framed transport.

    ``inbox`` is the pre-loaded byte stream the client reads. When the inbox is
    exhausted, reads either block forever (``block_when_empty=True``) or raise
    ``asyncio.IncompleteReadError`` to signal EOF/partial data. Everything the
    client writes is captured in ``outbox``.
    """

    def __init__(self, inbox: bytes = b"", *, block_when_empty: bool = False) -> None:
        self._inbox = bytearray(inbox)
        self.outbox = bytearray()
        self.block_when_empty = block_when_empty
        self.close_count = 0

    async def read_exactly(self, n: int) -> bytes:
        while len(self._inbox) < n:
            if self.block_when_empty:
                await asyncio.Event().wait()  # block until cancelled
            partial = bytes(self._inbox)
            self._inbox.clear()
            raise asyncio.IncompleteReadError(partial, n)
        chunk = bytes(self._inbox[:n])
        del self._inbox[:n]
        return chunk

    async def write(self, data: bytes) -> None:
        self.outbox.extend(data)

    async def close(self) -> None:
        self.close_count += 1


def _client(
    fake: FakeTransport, identity: BridgeIdentity | None = None
) -> BridgeClient:
    return BridgeClient(
        host="127.0.0.1",
        port=18811,
        identity=identity or create_bridge_identity(),
        transport_factory=lambda: fake,
    )


# --------------------------------------------------------------------------
# 15-17. open / hello lifecycle
# --------------------------------------------------------------------------


@async_test
async def test_open_sends_hello_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=True))
    identity = create_bridge_identity()
    client = _client(fake, identity)
    await client.open()
    frames = _parse_frames(bytes(fake.outbox))
    assert len(frames) >= 1
    hello = json.loads(frames[0])
    assert hello["protocol"] == _PROTO
    assert hello["kind"] == "hello"
    assert hello["token"] == identity.token


@async_test
async def test_wrong_token_hello_fails_and_closes() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=False))
    client = _client(fake)
    with pytest.raises(BridgeClientError) as exc:
        await client.open()
    assert exc.value.code == "bridge.unauthorized"
    assert fake.close_count >= 1


@async_test
async def test_request_before_open_is_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame())
    client = _client(fake)
    with pytest.raises(RuntimeError):
        await client.request(_make_request())


@async_test
async def test_request_after_failed_hello_is_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=False))
    client = _client(fake)
    with pytest.raises(BridgeClientError):
        await client.open()
    with pytest.raises(RuntimeError):
        await client.request(_make_request())


# --------------------------------------------------------------------------
# 18-21. request/response exchange
# --------------------------------------------------------------------------


@async_test
async def test_request_frame_is_canonical_json() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _success_frame("req_test"))
    client = _client(fake)
    await client.open()
    request = _make_request(request_id="req_test")
    await client.request(request)
    frames = _parse_frames(bytes(fake.outbox))
    # frames[0] is hello; frames[1] is the request payload bytes.
    assert frames[1] == request.to_json().encode("utf-8")


@async_test
async def test_response_request_id_mismatch_is_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _success_frame("other_id"))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test"))
    assert exc.value.code == "bridge.invalid_request"


@async_test
async def test_valid_response_returns_scene_query_result() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _success_frame("req_test"))
    client = _client(fake)
    await client.open()
    result = await client.request(_make_request(request_id="req_test"))
    assert isinstance(result, SceneQueryResult)
    assert result.binding.instance_id == "hou_instance_1"
    assert result.selected_nodes[0].path == "/obj/geo1"


@async_test
async def test_bridge_error_becomes_structured_exception() -> None:
    error = BridgeError(
        code="bridge.stale_scene",
        category="stale_scene",
        message_for_user="The Houdini scene changed; refresh before continuing.",
        retryable=True,
        technical_detail_ref="err_abc",
    )
    fake = FakeTransport(inbox=_ack_frame() + _error_frame("req_test", error))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test"))
    assert exc.value.code == "bridge.stale_scene"
    assert exc.value.category == "stale_scene"
    assert exc.value.message_for_user == "The Houdini scene changed; refresh before continuing."
    assert exc.value.retryable is True
    assert exc.value.technical_detail_ref == "err_abc"


# --------------------------------------------------------------------------
# 22-26. malformed response rejection
# --------------------------------------------------------------------------


def _bad_response_frame(mutator: dict[str, object], raw_payload: bytes | None = None) -> bytes:
    if raw_payload is not None:
        return _frame(raw_payload)
    base: dict[str, object] = {
        "protocol": _PROTO,
        "kind": "response",
        "request_id": "req_test",
        "ok": True,
        "result": _sample_result().to_dict(),
    }
    base.update(mutator)
    return _frame(_dumps(base))


@async_test
async def test_invalid_protocol_rejected() -> None:
    fake = FakeTransport(
        inbox=_ack_frame()
        + _bad_response_frame({"protocol": "eee.bridge/2"})
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError):
        await client.request(_make_request(request_id="req_test"))


@async_test
async def test_invalid_kind_rejected() -> None:
    fake = FakeTransport(
        inbox=_ack_frame() + _bad_response_frame({"kind": "event"})
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError):
        await client.request(_make_request(request_id="req_test"))


@async_test
async def test_invalid_json_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _frame(b"{not json"))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError):
        await client.request(_make_request(request_id="req_test"))


@async_test
async def test_invalid_utf8_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _frame(b"\xff\xfe\x00\x00bad"))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError):
        await client.request(_make_request(request_id="req_test"))


@async_test
async def test_duplicate_keys_rejected() -> None:
    result_json = _dumps(_sample_result().to_dict())
    dup = (
        '{"protocol":"eee.bridge/1","protocol":"eee.bridge/1",'
        '"kind":"response","request_id":"req_test","ok":true,'
        '"result":' + result_json + "}"
    ).encode("utf-8")
    fake = FakeTransport(inbox=_ack_frame() + _frame(dup))
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError):
        await client.request(_make_request(request_id="req_test"))


# --------------------------------------------------------------------------
# 27-29. framing failures
# --------------------------------------------------------------------------


@async_test
async def test_partial_frame_rejected() -> None:
    # Header claims 100 payload bytes but only 5 follow.
    partial = (100).to_bytes(4, "big") + b"short"
    fake = FakeTransport(inbox=_ack_frame() + partial)
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test"))
    assert exc.value.code == "bridge.not_available"


@async_test
async def test_oversize_frame_rejected() -> None:
    # 4-byte header encoding a length > 1 MiB; client rejects before reading.
    oversize = (MAX_MESSAGE_BYTES + 1).to_bytes(4, "big") + b"x" * 16
    fake = FakeTransport(inbox=_ack_frame() + oversize)
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test"))
    assert exc.value.code == "bridge.invalid_request"


@pytest.mark.parametrize(
    "length_bytes",
    [(0).to_bytes(4, "big"), (0xFFFFFFFF).to_bytes(4, "big")],
    ids=["zero-length", "huge-length"],
)
def test_bad_frame_length_rejected(length_bytes: bytes) -> None:
    async def body() -> None:
        fake = FakeTransport(inbox=_ack_frame() + length_bytes + b"x" * 8)
        client = _client(fake)
        await client.open()
        with pytest.raises(BridgeClientError) as exc:
            await client.request(_make_request(request_id="req_test"))
        assert exc.value.code == "bridge.invalid_request"

    asyncio.run(body())


# --------------------------------------------------------------------------
# 30-33. deadline / cancellation / EOF
# --------------------------------------------------------------------------


@async_test
async def test_deadline_exceeds_and_closes_transport() -> None:
    fake = FakeTransport(inbox=_ack_frame(), block_when_empty=True)
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test", deadline_ms=1))
    assert exc.value.code == "bridge.deadline_exceeded"
    assert exc.value.retryable is True
    assert fake.close_count >= 1


@async_test
async def test_cancellation_propagates_and_closes_transport() -> None:
    fake = FakeTransport(inbox=_ack_frame(), block_when_empty=True)
    client = _client(fake)
    await client.open()
    task = asyncio.create_task(client.request(_make_request(request_id="req_test")))
    # Let the request send and then hang on the response read.
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.close_count >= 1


@async_test
async def test_eof_becomes_not_available() -> None:
    # Inbox holds only the hello ack; the response read hits EOF.
    fake = FakeTransport(inbox=_ack_frame())
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.request(_make_request(request_id="req_test"))
    assert exc.value.code == "bridge.not_available"


# --------------------------------------------------------------------------
# 34-37. lifecycle / construction
# --------------------------------------------------------------------------


@async_test
async def test_close_is_idempotent() -> None:
    fake = FakeTransport(inbox=_ack_frame())
    client = _client(fake)
    await client.open()
    await client.close()
    await client.close()
    await client.close()
    assert fake.close_count == 1


@async_test
async def test_repeat_open_is_rejected() -> None:
    fake = FakeTransport(inbox=_ack_frame())
    client = _client(fake)
    await client.open()
    with pytest.raises(RuntimeError):
        await client.open()


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "10.0.0.1"])
def test_non_loopback_host_rejected(host: str) -> None:
    identity = create_bridge_identity()
    with pytest.raises((ValueError, TypeError)):
        BridgeClient(host=host, port=18811, identity=identity)


@pytest.mark.parametrize(
    "port",
    [True, 1.5, 0, 65536, -1, "8080"],
    ids=["bool", "float", "zero", "too-big", "negative", "str"],
)
def test_bad_port_rejected(port: object) -> None:
    identity = create_bridge_identity()
    with pytest.raises((ValueError, TypeError)):
        BridgeClient(host="127.0.0.1", port=port, identity=identity)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# 38-40. hygiene + DTO immutability + no background work
# --------------------------------------------------------------------------


@async_test
async def test_token_not_in_exception_text() -> None:
    fake = FakeTransport(inbox=_ack_frame(ok=False))
    identity = create_bridge_identity()
    client = _client(fake, identity)
    with pytest.raises(BridgeClientError) as exc:
        await client.open()
    assert identity.token not in exc.value.message_for_user
    assert identity.token not in str(exc.value)
    assert identity.token not in repr(exc.value)


@async_test
async def test_result_is_a_deeply_immutable_dto() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _success_frame("req_test"))
    client = _client(fake)
    await client.open()
    result = await client.request(_make_request(request_id="req_test"))
    assert isinstance(result, SceneQueryResult)
    # Frozen dataclass fields cannot be reassigned.
    with pytest.raises(AttributeError):
        result.binding = None  # type: ignore[misc]
    # selected_nodes is a tuple, and geometry_stats is a frozen mapping.
    assert type(result.selected_nodes) is tuple
    node = result.selected_nodes[0]
    with pytest.raises(TypeError):
        node.geometry_stats["points"] = 99  # type: ignore[index]


@async_test
async def test_no_retry_broadcast_or_background_task() -> None:
    fake = FakeTransport(inbox=_ack_frame() + _success_frame("req_test"))
    client = _client(fake)
    tasks_before = set(asyncio.all_tasks())
    await client.open()
    await client.request(_make_request(request_id="req_test"))
    await client.close()
    tasks_after = set(asyncio.all_tasks())
    # No leaked background tasks.
    assert tasks_after == tasks_before
    # Exactly one request frame was sent (no retry loop, no broadcast).
    frames = _parse_frames(bytes(fake.outbox))
    request_frames = [
        f for f in frames if json.loads(f).get("kind") == "request"
    ]
    assert len(request_frames) == 1


# ==========================================================================
# Task 15-D: construct the client from the bridge.token / discovery handoff
#
# The client must build its in-memory identity from the state directory's
# discovery (host/port + fingerprint) and ``bridge.token`` file, verify the
# fingerprint before connecting, and never fall back to env/CLI/Runtime tokens.
# ==========================================================================

from pathlib import Path

from eee_agent.houdini_bridge.auth import (
    BRIDGE_TOKEN_FILENAME,
    BridgeIdentityError,
    BridgeTokenError,
    write_bridge_identity_files,
)


def _publish_identity(state_dir: Path) -> "object":
    from eee_agent.houdini_bridge.auth import create_bridge_identity

    identity = create_bridge_identity()
    write_bridge_identity_files(identity, state_dir, host="127.0.0.1", port=49152)
    return identity


def test_from_state_dir_loads_identity_and_connects() -> None:
    import tempfile

    async def body(state_dir: Path) -> None:
        identity = _publish_identity(state_dir)
        fake = FakeTransport(inbox=_ack_frame())
        client = BridgeClient.from_state_dir(state_dir, transport_factory=lambda: fake)
        await client.open()
        # The hello frame carries the token loaded from the file (not an env/CLI value).
        frames = _parse_frames(bytes(fake.outbox))
        hello = json.loads(frames[0])
        assert hello["token"] == identity.token
        await client.close()

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(body(Path(d)))


def test_from_state_dir_missing_files_raises(tmp_path: Path) -> None:
    with pytest.raises(BridgeTokenError):
        BridgeClient.from_state_dir(tmp_path)


def test_from_state_dir_rejects_fingerprint_mismatch(tmp_path: Path) -> None:
    from eee_agent.houdini_bridge.auth import create_bridge_identity

    identity = _publish_identity(tmp_path)
    # Tamper the token file so its fingerprint no longer matches discovery.
    other = create_bridge_identity()
    (tmp_path / BRIDGE_TOKEN_FILENAME).write_bytes(other.token.encode("utf-8") + b"\n")
    with pytest.raises(BridgeIdentityError) as exc:
        BridgeClient.from_state_dir(tmp_path)
    assert identity.token not in str(exc.value)
    assert other.token not in str(exc.value)


def test_from_state_dir_ignores_env_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = _publish_identity(tmp_path)
    # An env var must never be used as a fallback for the Bridge token.
    monkeypatch.setenv("EEE_BRIDGE_TOKEN", "env-fallback-must-not-be-used")
    monkeypatch.setenv("BRIDGE_TOKEN", "also-not-used")

    async def body() -> None:
        fake = FakeTransport(inbox=_ack_frame())
        client = BridgeClient.from_state_dir(tmp_path, transport_factory=lambda: fake)
        await client.open()
        frames = _parse_frames(bytes(fake.outbox))
        hello = json.loads(frames[0])
        # Loaded strictly from the file, not the environment.
        assert hello["token"] == identity.token
        assert hello["token"] != "env-fallback-must-not-be-used"
        await client.close()

    asyncio.run(body())


def test_from_state_dir_reads_host_port_from_discovery(tmp_path: Path) -> None:
    from eee_agent.houdini_bridge.auth import create_bridge_identity

    identity = create_bridge_identity()
    write_bridge_identity_files(identity, tmp_path, host="127.0.0.1", port=51234)
    client = BridgeClient.from_state_dir(tmp_path)
    # The client binds to the loopback host/port advertised in discovery.
    assert client.host == "127.0.0.1"
    assert client.port == 51234


# ==========================================================================
# Task 16-C: hello capability negotiation on the client
# ==========================================================================

from eee_agent.houdini_bridge.changesets import CHANGESET_V1  # noqa: E402


@async_test
async def test_client_stores_advertised_capabilities() -> None:
    fake = FakeTransport(inbox=_ack_frame(caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    assert client.capabilities == ("changeset.v1",)
    assert CHANGESET_V1 in client.capabilities
    await client.close()


@async_test
async def test_client_legacy_ack_yields_empty_capabilities() -> None:
    fake = FakeTransport(inbox=_ack_frame())  # no capabilities key
    client = _client(fake)
    await client.open()
    assert client.capabilities == ()
    await client.close()


@pytest.mark.parametrize(
    "bad_caps",
    [
        "changeset.v1",  # not a list
        ["changeset.v1", "changeset.v1"],  # duplicate
        ["scene.v1", "changeset.v1"],  # unsorted
        ["changeset.v1", 7],  # non-string element
        ["BADCAP"],  # bad grammar
    ],
    ids=["non-list", "duplicate", "unsorted", "non-string", "bad-grammar"],
)
def test_client_malformed_capability_ack_fails_closed(bad_caps: object) -> None:
    async def body() -> None:
        fake = FakeTransport(inbox=_ack_frame(caps=bad_caps))  # type: ignore[arg-type]
        client = _client(fake)
        with pytest.raises(BridgeClientError):
            await client.open()

    asyncio.run(body())


@async_test
async def test_close_clears_capabilities() -> None:
    fake = FakeTransport(inbox=_ack_frame(caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    assert client.capabilities == ("changeset.v1",)
    await client.close()
    assert client.capabilities == ()


# ===========================================================================
# Task 16-B2b-1: workspace capability and typed inspection
# ===========================================================================

from eee_agent.houdini_bridge.workspaces import (  # noqa: E402
    WORKSPACE_V1,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceInspectResult,
    WorkspaceNodeObservation,
)


def _workspace_request(request_id: str = "req_workspace") -> WorkspaceInspectRequest:
    return WorkspaceInspectRequest(
        request_id=request_id,
        deadline_ms=5000,
        scene_epoch=1,
        mode="selection",
        manifest=None,
    )


def _workspace_result() -> WorkspaceInspectResult:
    return WorkspaceInspectResult.build(
        binding=SceneBinding(
            instance_id="hou_instance_1",
            scene_epoch=1,
            hip_path=None,
            observed_revision="sha256:workspace-scene",
        ),
        mode="selection",
        observations=(
            WorkspaceNodeObservation(
                path="/obj/geo1",
                node_type="geo",
                parent_path="/obj",
                is_locked=False,
                workspace_id=None,
                node_id=None,
                capability=None,
                role=None,
                schema_version=None,
                created_by_run=None,
            ),
        ),
    )


def _workspace_success_frame(request_id: str = "req_workspace") -> bytes:
    response = WorkspaceInspectResponse(
        request_id=request_id,
        result=_workspace_result(),
        error=None,
    )
    return _frame(response.to_json())


@async_test
async def test_workspace_capability_is_stored_with_existing_capability() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(caps=["changeset.v1", "workspace.v1"])
    )
    client = _client(fake)
    await client.open()
    assert client.capabilities == (CHANGESET_V1, WORKSPACE_V1)
    await client.close()


@async_test
async def test_workspace_inspect_without_capability_sends_no_request_frame() -> None:
    fake = FakeTransport(inbox=_ack_frame(caps=["changeset.v1"]))
    client = _client(fake)
    await client.open()
    assert len(_parse_frames(bytes(fake.outbox))) == 1  # hello only
    with pytest.raises(BridgeClientError) as exc:
        await client.inspect_workspace(_workspace_request())
    assert exc.value.code == "bridge.capability_unavailable"
    assert len(_parse_frames(bytes(fake.outbox))) == 1
    await client.close()


@async_test
async def test_workspace_inspect_round_trip_returns_typed_result() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(caps=["changeset.v1", "workspace.v1"])
        + _workspace_success_frame()
    )
    client = _client(fake)
    await client.open()
    result = await client.inspect_workspace(_workspace_request())
    assert result == _workspace_result()
    frames = _parse_frames(bytes(fake.outbox))
    assert json.loads(frames[1])["operation"] == "workspace.inspect"
    await client.close()


@async_test
async def test_workspace_inspect_request_id_mismatch_aborts_connection() -> None:
    fake = FakeTransport(
        inbox=_ack_frame(caps=["changeset.v1", "workspace.v1"])
        + _workspace_success_frame("other")
    )
    client = _client(fake)
    await client.open()
    with pytest.raises(BridgeClientError) as exc:
        await client.inspect_workspace(_workspace_request())
    assert exc.value.code == "bridge.invalid_request"
    assert fake.close_count >= 1


# ===========================================================================
# Task 16-B2b-1: production workspace provider over discovered short clients
# ===========================================================================

from eee_agent.houdini_bridge.workspace_provider import (  # noqa: E402
    BridgeWorkspaceFactProvider,
)
from eee_agent.houdini_bridge.workspaces import (  # noqa: E402
    WorkspaceInspectionConflict,
    WorkspaceInspectionUnavailable,
)


class _ProviderClient:
    def __init__(
        self,
        *,
        result: WorkspaceInspectResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result or _workspace_result()
        self.error = error
        self.requests: list[WorkspaceInspectRequest] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "_ProviderClient":
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.exited += 1

    async def inspect_workspace(
        self, request: WorkspaceInspectRequest
    ) -> WorkspaceInspectResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


def test_workspace_provider_selection_uses_one_discovered_short_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _ProviderClient()
    seen: list[Path] = []

    def from_state_dir(state_dir: Path) -> _ProviderClient:
        seen.append(Path(state_dir))
        return fake

    monkeypatch.setattr(BridgeClient, "from_state_dir", staticmethod(from_state_dir))
    provider = BridgeWorkspaceFactProvider(tmp_path, deadline_ms=4321)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    result = asyncio.run(provider.inspect_selection(7))
    assert result == fake.result
    assert seen == [tmp_path]
    assert fake.entered == fake.exited == 1
    request = fake.requests[0]
    assert request.mode == "selection"
    assert request.manifest is None
    assert request.scene_epoch == 7
    assert request.deadline_ms == 4321


def test_workspace_provider_manifest_carries_exact_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _ProviderClient()
    monkeypatch.setattr(
        BridgeClient,
        "from_state_dir",
        staticmethod(lambda _state_dir: fake),
    )
    from datetime import datetime, timezone

    from eee_agent.changesets import OwnedNodeRef, WorkspaceManifest

    owned = OwnedNodeRef(
        node_id="node_root",
        path="/obj/owned",
        node_type="geo",
        parent_path="/obj",
        capability="modeling",
        role="root",
    )
    manifest = WorkspaceManifest.build(
        workspace_id=f"ws_{'2' * 32}",
        session_id=f"ses_{'0' * 32}",
        instance_id="hou_instance_1",
        scene_epoch=1,
        roots=(owned,),
        nodes=(owned,),
        created_by_run=f"run_{'1' * 32}",
        updated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    provider = BridgeWorkspaceFactProvider(tmp_path)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    asyncio.run(provider.inspect_manifest(manifest, None))
    request = fake.requests[0]
    assert request.mode == "manifest"
    assert request.manifest is manifest
    assert request.scene_epoch is None


def test_workspace_provider_missing_handoff_is_ordinary_unavailable(
    tmp_path: Path,
) -> None:
    provider = BridgeWorkspaceFactProvider(tmp_path)
    with pytest.raises(WorkspaceInspectionUnavailable):
        asyncio.run(provider.inspect_selection(None))


def test_workspace_provider_maps_exact_identity_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _ProviderClient(
        error=BridgeClientError(
            code="workspace.identity_conflict",
            category="validation",
            message_for_user="ambiguous",
            retryable=False,
        )
    )
    monkeypatch.setattr(
        BridgeClient,
        "from_state_dir",
        staticmethod(lambda _state_dir: fake),
    )
    provider = BridgeWorkspaceFactProvider(tmp_path)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    with pytest.raises(WorkspaceInspectionConflict):
        asyncio.run(provider.inspect_selection(None))
    assert fake.exited == 1


def test_workspace_provider_does_not_hide_protocol_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original = BridgeClientError(
        code="bridge.invalid_request",
        category="protocol",
        message_for_user="bad response",
        retryable=False,
    )
    fake = _ProviderClient(error=original)
    monkeypatch.setattr(
        BridgeClient,
        "from_state_dir",
        staticmethod(lambda _state_dir: fake),
    )
    provider = BridgeWorkspaceFactProvider(tmp_path)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    with pytest.raises(BridgeClientError) as exc:
        asyncio.run(provider.inspect_selection(None))
    assert exc.value is original


class _HangingProviderClient(_ProviderClient):
    async def __aenter__(self) -> "_HangingProviderClient":
        await asyncio.Event().wait()
        return self  # pragma: no cover


def test_workspace_provider_deadline_covers_open_and_hello(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _HangingProviderClient()
    monkeypatch.setattr(
        BridgeClient,
        "from_state_dir",
        staticmethod(lambda _state_dir: fake),
    )
    provider = BridgeWorkspaceFactProvider(tmp_path, deadline_ms=10)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)

    async def scenario() -> None:
        with pytest.raises(WorkspaceInspectionUnavailable):
            await asyncio.wait_for(provider.inspect_selection(None), timeout=0.5)

    asyncio.run(scenario())


class _NetworkFailingProviderClient(_ProviderClient):
    async def __aenter__(self) -> "_NetworkFailingProviderClient":
        raise OSError("connection refused")


def test_workspace_provider_maps_network_oserror_to_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _NetworkFailingProviderClient()
    monkeypatch.setattr(
        BridgeClient,
        "from_state_dir",
        staticmethod(lambda _state_dir: fake),
    )
    provider = BridgeWorkspaceFactProvider(tmp_path)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    with pytest.raises(WorkspaceInspectionUnavailable):
        asyncio.run(provider.inspect_selection(None))


def test_workspace_provider_does_not_hide_malformed_existing_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eee_agent.houdini_bridge.auth import BridgeIdentityError

    malformed = BridgeIdentityError("fingerprint mismatch")

    def fail_identity(_state_dir: Path) -> _ProviderClient:
        raise malformed

    monkeypatch.setattr(
        BridgeClient, "from_state_dir", staticmethod(fail_identity)
    )
    provider = BridgeWorkspaceFactProvider(tmp_path)
    monkeypatch.setattr(provider, "_handoff_exists", lambda: True)
    with pytest.raises(BridgeIdentityError) as exc:
        asyncio.run(provider.inspect_selection(None))
    assert exc.value is malformed
