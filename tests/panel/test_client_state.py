from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from eee_agent.panel.client_state import (
    PanelClientError,
    RuntimeCursorBook,
    build_command,
    choose_active_session,
    load_runtime_credentials,
    parse_runtime_message,
)


def _write_identity(state: Path, *, token: str = "secret-token", **overrides) -> None:
    state.mkdir(parents=True, exist_ok=True)
    discovery = {
        "protocol": "eee.runtime/1",
        "host": "127.0.0.1",
        "port": 32123,
        "pid": 123,
        "process_nonce": "nonce",
        "token_file": "runtime.token",
        "token_fingerprint": hashlib.sha256(token.encode()).hexdigest()[:12],
        "started_at": "2026-07-16T12:00:00+00:00",
    }
    discovery.update(overrides)
    (state / "runtime.json").write_text(json.dumps(discovery), encoding="utf-8")
    (state / "runtime.token").write_text(token, encoding="utf-8")


def test_load_runtime_credentials_verifies_loopback_and_fingerprint(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_identity(state)
    credentials = load_runtime_credentials(state)
    assert credentials.websocket_url == "ws://127.0.0.1:32123"
    assert credentials.process_nonce == "nonce"
    assert "secret-token" not in repr(credentials)


@pytest.mark.parametrize(
    "overrides",
    [
        {"protocol": "eee.runtime/2"},
        {"host": "localhost"},
        {"port": 0},
        {"port": True},
        {"token_file": "other.token"},
        {"token_fingerprint": "not-hex"},
    ],
)
def test_load_runtime_credentials_rejects_invalid_discovery(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    state = tmp_path / "state"
    _write_identity(state, **overrides)
    with pytest.raises(PanelClientError):
        load_runtime_credentials(state)


def test_load_runtime_credentials_rejects_fingerprint_mismatch_without_leak(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _write_identity(state)
    (state / "runtime.token").write_text("different-secret", encoding="utf-8")
    with pytest.raises(PanelClientError) as exc:
        load_runtime_credentials(state)
    assert "different-secret" not in str(exc.value)


def test_load_runtime_credentials_rejects_duplicate_discovery_keys(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    token = "secret-token"
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
    (state / "runtime.json").write_text(
        '{"protocol":"eee.runtime/1","protocol":"eee.runtime/1",'
        '"host":"127.0.0.1","port":1,"pid":1,"process_nonce":"n",'
        '"token_file":"runtime.token","token_fingerprint":"'
        + fingerprint
        + '","started_at":"2026-07-16T12:00:00+00:00"}',
        encoding="utf-8",
    )
    (state / "runtime.token").write_text(token, encoding="utf-8")
    with pytest.raises(PanelClientError):
        load_runtime_credentials(state)


def test_build_command_is_canonical_and_read_only() -> None:
    text = build_command("req_1", "runtime.ping", {})
    assert text == (
        '{"kind":"command","payload":{},"protocol":"eee.runtime/1",'
        '"request_id":"req_1","type":"runtime.ping"}'
    )
    with pytest.raises(PanelClientError):
        build_command("req_2", "run.start", {})
    with pytest.raises(PanelClientError):
        build_command("req_3", "changeset.approve", {})


def test_parse_runtime_message_accepts_response_and_event() -> None:
    response = parse_runtime_message(
        '{"protocol":"eee.runtime/1","kind":"response",'
        '"request_id":"r","ok":true,"result":{"pong":true}}'
    )
    assert response["ok"] is True
    event = parse_runtime_message(
        '{"protocol":"eee.runtime/1","kind":"event","event_id":"evt_x",'
        '"session_id":"ses_x","run_id":null,"seq":4,'
        '"timestamp":"2026-07-16T12:00:00+00:00","type":"run.state",'
        '"payload":{},"schema_version":1}'
    )
    assert event["seq"] == 4


def test_parse_runtime_message_rejects_duplicate_and_incompatible() -> None:
    with pytest.raises(PanelClientError):
        parse_runtime_message(
            '{"protocol":"eee.runtime/1","kind":"response","kind":"event",'
            '"request_id":"r","ok":true}'
        )
    with pytest.raises(PanelClientError):
        parse_runtime_message(
            '{"protocol":"eee.runtime/2","kind":"response",'
            '"request_id":"r","ok":true}'
        )


def _session(session_id: str, updated_at: str, *, status: str = "active") -> dict:
    return {
        "session_id": session_id,
        "title": session_id,
        "status": status,
        "updated_at": updated_at,
    }


def test_choose_active_session_prefers_current_then_latest() -> None:
    sessions = [
        _session("ses_a", "2026-07-16T10:00:00+00:00"),
        _session("ses_b", "2026-07-16T11:00:00+00:00"),
        _session("ses_c", "2026-07-16T12:00:00+00:00", status="archived"),
    ]
    assert choose_active_session(sessions, "ses_a")["session_id"] == "ses_a"
    assert choose_active_session(sessions)["session_id"] == "ses_b"
    assert choose_active_session([sessions[2]]) is None


def test_choose_active_session_tie_breaks_by_session_id() -> None:
    timestamp = "2026-07-16T10:00:00+00:00"
    chosen = choose_active_session(
        [_session("ses_a", timestamp), _session("ses_b", timestamp)]
    )
    assert chosen["session_id"] == "ses_b"


def test_cursor_book_is_per_session_monotonic_and_ignores_control_events() -> None:
    cursors = RuntimeCursorBook()
    persisted = {
        "kind": "event",
        "session_id": "ses_a",
        "seq": 2,
    }
    assert cursors.observe(persisted) is True
    assert cursors.observe(persisted) is False
    assert cursors.observe({**persisted, "seq": 1}) is False
    assert cursors.observe({**persisted, "session_id": "ses_b", "seq": 1}) is True
    assert cursors.observe({**persisted, "seq": None}) is False
    assert cursors.last_seq("ses_a") == 2
    assert cursors.last_seq("ses_b") == 1
