"""Task 11: strict Runtime protocol parsing and serialization tests."""

from __future__ import annotations

import json

import pytest

from eee_agent.core import AgentError, AgentException, ErrorCategory
from eee_agent.runtime.protocol import (
    COMMAND_TYPES,
    DEFERRED_COMMAND_TYPES,
    MAX_MESSAGE_BYTES,
    PROTOCOL,
    RuntimeCommand,
    encode_envelope,
    error_response,
    event_envelope,
    parse_command,
    success_response,
)

_NOW = "2026-07-14T02:30:00+00:00"


def _command(**overrides) -> dict:
    base = {
        "protocol": "eee.runtime/1",
        "kind": "command",
        "request_id": "r1",
        "type": "runtime.ping",
        "payload": {},
    }
    base.update(overrides)
    return base


def _err(exc: AgentException) -> tuple[str, str]:
    return (exc.value.error.code, exc.value.error.category.value)


# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------


def test_protocol_constant_is_exact() -> None:
    assert PROTOCOL == "eee.runtime/1"


def test_max_message_bytes_is_one_mebibyte() -> None:
    assert MAX_MESSAGE_BYTES == 1_048_576


def test_command_types_match_plan() -> None:
    assert COMMAND_TYPES == frozenset(
        {
            "runtime.ping",
            "session.list",
            "session.create",
            "session.rename",
            "session.archive",
            "session.delete",
            "session.subscribe",
            "session.snapshot",
            "events.replay",
            "run.start",
            "run.stop",
            "run.force_stop",
            "changeset.list",
            "changeset.approve",
            "changeset.reject",
            "workspace.create",
            "workspace.bind",
            "workspace.switch",
            "workspace.inspect",
        }
    )


def test_deferred_command_types_match_plan() -> None:
    assert DEFERRED_COMMAND_TYPES == frozenset(
        {
            "session.fork",
            "visual_policy.resolve",
            "artifact.reveal",
            "artifact.open",
            "artifact.copy_reference",
        }
    )


# --------------------------------------------------------------------------
# parse_command: happy path
# --------------------------------------------------------------------------


def test_parse_command_accepts_str() -> None:
    command = parse_command(json.dumps(_command()))
    assert type(command) is RuntimeCommand
    assert command.request_id == "r1"
    assert command.command_type == "runtime.ping"
    assert command.payload == {}


def test_parse_command_accepts_utf8_bytes() -> None:
    payload = {"msg": "中文"}
    command = parse_command(json.dumps(_command(payload=payload)).encode("utf-8"))
    assert command.payload == payload


def test_parse_command_payload_is_independent_copy() -> None:
    source = {"a": [1, 2]}
    command = parse_command(json.dumps(_command(payload=source)))
    command.payload["a"].append(3)
    command.payload["b"] = 1
    assert source == {"a": [1, 2]}


def test_parse_command_preserves_all_implemented_commands() -> None:
    for name in COMMAND_TYPES:
        command = parse_command(json.dumps(_command(type=name)))
        assert command.command_type == name


def test_parse_command_preserves_deferred_commands() -> None:
    # Deferred commands are parsed (not rejected) so the server can return
    # runtime.capability_unavailable.
    for name in DEFERRED_COMMAND_TYPES:
        command = parse_command(json.dumps(_command(type=name)))
        assert command.command_type == name


# --------------------------------------------------------------------------
# parse_command: rejections
# --------------------------------------------------------------------------


def test_parse_command_rejects_invalid_json() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command("{not json")
    assert _err(exc) == ("protocol.invalid_json", "protocol")


def test_parse_command_rejects_invalid_utf8() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(b"\xff\xfe\x00bad")
    assert _err(exc) == ("protocol.invalid_json", "protocol")


def test_parse_command_rejects_non_object() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps([1, 2, 3]))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_missing_field() -> None:
    bad = _command()
    del bad["request_id"]
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(bad))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_extra_field() -> None:
    bad = _command()
    bad["unexpected"] = 1
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(bad))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_wrong_type_for_protocol() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(protocol=1)))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_wrong_type_for_kind() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(kind=1)))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_non_string_request_id() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(request_id=1)))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_non_string_type() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(type=1)))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_non_object_payload() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(payload=[1, 2])))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_empty_request_id() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(request_id="")))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_129_char_request_id() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(request_id="r" * 129)))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_accepts_128_char_request_id() -> None:
    command = parse_command(json.dumps(_command(request_id="r" * 128)))
    assert len(command.request_id) == 128


def test_parse_command_rejects_wrong_kind() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(kind="response")))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_incompatible_protocol_major() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(protocol="eee.runtime/2")))
    assert _err(exc) == ("protocol.incompatible_version", "protocol")


def test_parse_command_rejects_non_runtime_protocol() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(protocol="something/else")))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_oversized_message_by_utf8_bytes() -> None:
    # Build a valid command whose UTF-8 byte length exceeds 1 MiB.
    padding = "x" * (MAX_MESSAGE_BYTES + 1)
    raw = json.dumps(_command(payload={"padding": padding}))
    assert len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES
    with pytest.raises(AgentException) as exc:
        parse_command(raw)
    assert _err(exc) == ("validation.payload_too_large", "validation")


def test_parse_command_rejects_bytes_oversized() -> None:
    raw = json.dumps(_command(payload={"padding": "x" * MAX_MESSAGE_BYTES})).encode(
        "utf-8"
    )
    assert len(raw) > MAX_MESSAGE_BYTES
    with pytest.raises(AgentException) as exc:
        parse_command(raw)
    assert _err(exc) == ("validation.payload_too_large", "validation")


def test_parse_command_rejects_unknown_command() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(type="totally.unknown")))
    assert _err(exc) == ("protocol.unknown_command", "protocol")


def test_parse_command_rejects_non_finite_float_in_payload() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(json.dumps(_command(payload={"x": float("nan")}), allow_nan=True))
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_overflow_infinity_in_payload() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command('{"protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{"x":1e400}}')
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_error_messages_do_not_leak_raw_detail() -> None:
    with pytest.raises(AgentException) as exc:
        parse_command("{this is not json at all and reveals <secret>}")
    assert "<secret>" not in exc.value.error.message_for_user
    assert "<secret>" not in str(exc.value.error.to_dict())


# --------------------------------------------------------------------------
# responses and envelopes
# --------------------------------------------------------------------------


def test_success_response_shape() -> None:
    resp = success_response("r1", {"pong": True})
    assert resp == {
        "protocol": PROTOCOL,
        "kind": "response",
        "request_id": "r1",
        "ok": True,
        "result": {"pong": True},
    }


def test_success_response_returns_fresh_objects() -> None:
    a = success_response("r1", {})
    b = success_response("r1", {})
    assert a == b
    assert a is not b
    assert a["result"] is not b["result"]


def test_error_response_equals_agent_error_to_dict() -> None:
    error = AgentError(
        code="protocol.unknown_command",
        category=ErrorCategory.PROTOCOL,
        message_for_user="The Runtime command is unknown.",
    )
    resp = error_response("r9", error)
    assert resp["protocol"] == PROTOCOL
    assert resp["kind"] == "response"
    assert resp["request_id"] == "r9"
    assert resp["ok"] is False
    assert resp["error"] == error.to_dict()
    assert resp["error"] is not error.to_dict()  # fresh dict


def test_error_response_returns_fresh_objects() -> None:
    error = AgentError(
        code="protocol.unknown_command",
        category=ErrorCategory.PROTOCOL,
        message_for_user="x",
    )
    a = error_response("r1", error)
    b = error_response("r1", error)
    assert a == b
    assert a is not b
    assert a["error"] is not b["error"]


def test_event_envelope_shape_with_run_id() -> None:
    env = event_envelope(
        event_id="evt_1",
        session_id="ses_1",
        run_id="run_1",
        seq=5,
        timestamp=_NOW,
        event_type="run.state_changed",
        payload={"from": "Planning", "to": "Finalizing"},
    )
    assert env == {
        "protocol": PROTOCOL,
        "kind": "event",
        "event_id": "evt_1",
        "session_id": "ses_1",
        "run_id": "run_1",
        "seq": 5,
        "timestamp": _NOW,
        "type": "run.state_changed",
        "payload": {"from": "Planning", "to": "Finalizing"},
        "schema_version": 1,
    }


def test_event_envelope_nullable_run_id() -> None:
    env = event_envelope(
        event_id="evt_1",
        session_id="ses_1",
        run_id=None,
        seq=1,
        timestamp=_NOW,
        event_type="session.created",
        payload={},
    )
    assert env["run_id"] is None
    assert env["seq"] == 1
    assert env["schema_version"] == 1


def test_event_envelope_returns_fresh_objects() -> None:
    a = event_envelope(
        event_id="e", session_id="s", run_id=None, seq=1,
        timestamp=_NOW, event_type="t", payload={},
    )
    b = event_envelope(
        event_id="e", session_id="s", run_id=None, seq=1,
        timestamp=_NOW, event_type="t", payload={},
    )
    assert a == b
    assert a is not b
    assert a["payload"] is not b["payload"]


def test_encode_envelope_is_compact_sorted_utf8() -> None:
    env = {"b": 1, "a": "中文", "c": True}
    encoded = encode_envelope(env)
    assert encoded == b'{"a":"\xe4\xb8\xad\xe6\x96\x87","b":1,"c":true}'
    assert encoded.decode("utf-8") == '{"a":"中文","b":1,"c":true}'


def test_encode_envelope_does_not_modify_input() -> None:
    env = {"b": 1, "a": [1, 2]}
    original = {"b": 1, "a": [1, 2]}
    encode_envelope(env)
    assert env == original


def test_encode_envelope_is_deterministic() -> None:
    env = {"z": 1, "a": 2, "m": [3, 2, 1]}
    assert encode_envelope(env) == encode_envelope(
        {"m": [3, 2, 1], "a": 2, "z": 1}
    )


# --------------------------------------------------------------------------
# strict-envelope regressions (Codex round 5)
# --------------------------------------------------------------------------


def _json_with_duplicate_top_level(field: str) -> str:
    parts: list[str] = []

    def emit(k: str, v: str) -> None:
        parts.append(f'"{k}":{v}')

    emit("protocol", '"eee.runtime/1"')
    if field == "protocol":
        emit("protocol", '"eee.runtime/1"')
    emit("kind", '"command"')
    emit("request_id", '"r1"')
    if field == "request_id":
        emit("request_id", '"r2"')
    emit("type", '"runtime.ping"')
    if field == "type":
        emit("type", '"runtime.ping"')
    emit("payload", "{}")
    if field == "payload":
        emit("payload", "{}")
    return "{" + ",".join(parts) + "}"


@pytest.mark.parametrize(
    "field", ["protocol", "request_id", "type", "payload"]
)
def test_parse_command_rejects_duplicate_top_level_key(field: str) -> None:
    raw = _json_with_duplicate_top_level(field)
    with pytest.raises(AgentException) as exc:
        parse_command(raw)
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_parse_command_rejects_duplicate_nested_payload_key() -> None:
    raw = (
        '{"protocol":"eee.runtime/1","kind":"command","request_id":"r1",'
        '"type":"runtime.ping","payload":{"a":1,"a":2}}'
    )
    with pytest.raises(AgentException) as exc:
        parse_command(raw)
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_duplicate_key_error_does_not_leak_values() -> None:
    secret = "secret-id-12345"
    raw = (
        '{"protocol":"eee.runtime/1","protocol":"eee.runtime/1",'
        '"kind":"command","request_id":"' + secret + '","request_id":"' + secret + '",'
        '"type":"runtime.ping","payload":{}}'
    )
    with pytest.raises(AgentException) as exc:
        parse_command(raw)
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")
    assert secret not in exc.value.error.message_for_user
    assert secret not in str(exc.value.error.to_dict())


class _StrSub(str):
    pass


class _BytesSub(bytes):
    pass


@pytest.mark.parametrize(
    "bad",
    [
        bytearray(b'{"protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{}}'),
        memoryview(b'{"protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{}}'),
        _StrSub('{"protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{}}'),
        _BytesSub(b'{"protocol":"eee.runtime/1","kind":"command","request_id":"r1","type":"runtime.ping","payload":{}}'),
    ],
    ids=["bytearray", "memoryview", "str-subclass", "bytes-subclass"],
)
def test_parse_command_rejects_non_str_bytes_types(bad) -> None:
    with pytest.raises(AgentException) as exc:
        parse_command(bad)
    assert _err(exc) == ("protocol.invalid_envelope", "protocol")


def test_success_response_deep_snapshots_result() -> None:
    result = {"a": [1, 2], "b": {"c": 3}}
    resp = success_response("r1", result)
    # Mutate the caller's object after construction.
    result["a"].append(3)
    result["b"]["c"] = 99
    result["new"] = 1
    assert resp["result"] == {"a": [1, 2], "b": {"c": 3}}


def test_success_response_calls_do_not_share_nested_objects() -> None:
    r1 = success_response("r1", {"a": [1]})
    r2 = success_response("r1", {"a": [1]})
    r1["result"]["a"].append(2)
    assert r2["result"]["a"] == [1]


def test_event_envelope_deep_snapshots_payload() -> None:
    payload = {"x": [1, 2], "y": {"z": 3}}
    env = event_envelope(
        event_id="e",
        session_id="s",
        run_id=None,
        seq=1,
        timestamp=_NOW,
        event_type="t",
        payload=payload,
    )
    payload["x"].append(3)
    payload["y"]["z"] = 99
    payload["new"] = 1
    assert env["payload"] == {"x": [1, 2], "y": {"z": 3}}


def test_event_envelope_calls_do_not_share_nested_payload() -> None:
    a = event_envelope(
        event_id="e", session_id="s", run_id=None, seq=1,
        timestamp=_NOW, event_type="t", payload={"a": [1]},
    )
    b = event_envelope(
        event_id="e", session_id="s", run_id=None, seq=1,
        timestamp=_NOW, event_type="t", payload={"a": [1]},
    )
    a["payload"]["a"].append(2)
    assert b["payload"]["a"] == [1]


@pytest.mark.parametrize(
    "bad_result",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {1, 2},  # non-JSON type
    ],
    ids=["nan", "inf", "set"],
)
def test_success_response_rejects_non_json_result(bad_result) -> None:
    with pytest.raises((TypeError, ValueError)):
        success_response("r1", bad_result)


def test_success_response_rejects_cycle_in_result() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises((TypeError, ValueError)):
        success_response("r1", cyclic)


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": {1, 2}},
    ],
    ids=["nan", "inf", "set"],
)
def test_event_envelope_rejects_non_json_payload(bad_payload) -> None:
    with pytest.raises((TypeError, ValueError)):
        event_envelope(
            event_id="e",
            session_id="s",
            run_id=None,
            seq=1,
            timestamp=_NOW,
            event_type="t",
            payload=bad_payload,
        )


def test_event_envelope_rejects_cycle_in_payload() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises((TypeError, ValueError)):
        event_envelope(
            event_id="e",
            session_id="s",
            run_id=None,
            seq=1,
            timestamp=_NOW,
            event_type="t",
            payload=cyclic,
        )
