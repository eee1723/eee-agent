"""Connection management for the Houdini RPC (rpyc) bridge.

Iron rule: NOTHING proxied is returned to callers. Tools go through ``run()``,
which executes a callable with the live ``hou`` proxy and lets ``serialize`` turn
the results into plain Python. The agent therefore never holds an rpyc proxy and
can never trip the "proxies don't support operators" trap.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple, TypeVar

import rpyc

from eee_agent.config import rpc_config

T = TypeVar("T")


class _State:
    conn: Optional[object] = None
    hou: Optional[object] = None


_state = _State()


def connect(host: Optional[str] = None, port: Optional[int] = None):
    """Connect to the Houdini RPC server and cache the ``hou`` proxy."""
    cfg = rpc_config()
    host = host or cfg.host
    port = int(port or cfg.port)
    conn = rpyc.classic.connect(host, port)
    # Houdini ops (e.g. loading a .hip) can take arbitrarily long.
    conn._config.update({"sync_request_timeout": None})
    _state.conn = conn
    _state.hou = conn.modules["hou"]
    return _state.hou


def disconnect() -> None:
    try:
        if _state.conn is not None:
            _state.conn.close()
    except Exception:
        pass
    _state.conn = None
    _state.hou = None


def is_connected() -> bool:
    return _state.hou is not None


def get_hou():
    """Return the cached ``hou`` proxy, connecting lazily on first use."""
    if _state.hou is None:
        connect()
    return _state.hou


def run(fn: Callable[[object], T]) -> Tuple[Optional[T], Optional[str]]:
    """Run ``fn(hou)`` and return ``(result, None)`` or ``(None, error_text)``.

    All exceptions — Houdini ``hou.OperationFailed``, rpyc network errors, etc. —
    are captured here so tools always return a structured dict to the agent.
    """
    try:
        return fn(get_hou()), None
    except Exception as exc:  # noqa: BLE001 — bridge must never raise to the agent
        name = type(exc).__name__
        msg = str(exc)
        # A dead connection should force a reconnect on the next call.
        if not _looks_alive():
            disconnect()
        return None, f"{name}: {msg}"


def _looks_alive() -> bool:
    """Best-effort liveness check (cheap: no network round-trip, just state)."""
    return _state.conn is not None and _state.hou is not None
