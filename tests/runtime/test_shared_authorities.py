"""Drift guards for constants and structured errors with a single authority.

Each assertion here replaces what used to be a comment asking two copies to
stay equal; if a future change re-introduces a second copy, these tests fail.
"""
from __future__ import annotations


def test_terminal_run_statuses_single_authority() -> None:
    from eee_agent.runtime import events, models, runs, service

    assert runs._TERMINAL_STATES == models.TERMINAL_RUN_STATUSES
    assert service._TERMINAL_STATUSES == models.TERMINAL_RUN_STATUSES
    assert set(events._TERMINAL_RUN_VALUES) == {
        status.value for status in models.TERMINAL_RUN_STATUSES
    }


def test_interrupted_error_shared_between_repository_and_service() -> None:
    from eee_agent.runtime import runs, service

    assert service._INTERRUPTED_ERROR is runs._INTERRUPTED_ERROR


def test_internal_failure_error_shared_between_server_and_service() -> None:
    from eee_agent.runtime import server, service

    assert server._INTERNAL_FAILURE_ERROR is service._RUNTIME_FAILURE_ERROR


def test_panel_protocol_constants_mirrored_not_duplicated() -> None:
    from eee_agent.panel import client_state
    from eee_agent.runtime import protocol

    assert client_state.PROTOCOL == protocol.PROTOCOL
    assert client_state.MAX_MESSAGE_BYTES == protocol.MAX_MESSAGE_BYTES


def test_panel_command_subset_of_protocol_commands() -> None:
    from eee_agent.panel import client_state
    from eee_agent.runtime import protocol

    assert client_state._PANEL_COMMANDS <= protocol.COMMAND_TYPES
    # Regression: knowledge.rebuild was omitted while the panel sent it,
    # making the Rebuild KB button fail client-side before any frame left.
    assert "knowledge.rebuild" in client_state._PANEL_COMMANDS


def test_empty_session_title_mirrored_between_server_and_panel() -> None:
    from eee_agent.panel import client_state
    from eee_agent.runtime import sessions

    assert client_state.EMPTY_SESSION_TITLE == sessions.EMPTY_SESSION_TITLE
