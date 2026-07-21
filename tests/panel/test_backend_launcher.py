"""Backend launcher contract: probe, spawn, wait, diagnose (Qt-free)."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

from eee_agent.panel.client_state import (
    RUNTIME_DISCOVERY_FILENAME,
    RUNTIME_TOKEN_FILENAME,
)
from houdini_side.runtime_panel import backend_launcher as bl


def _write_valid_discovery(state: Path, token: str = "t0ken",
                           port: int = 45678) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / RUNTIME_TOKEN_FILENAME).write_text(token, encoding="utf-8")
    fingerprint = hashlib.sha256(token.encode()).hexdigest()[:12]
    (state / RUNTIME_DISCOVERY_FILENAME).write_text(json.dumps({
        "protocol": "eee.runtime/1", "host": "127.0.0.1", "port": port,
        "pid": 1234, "process_nonce": "nonce",
        "token_file": RUNTIME_TOKEN_FILENAME,
        "token_fingerprint": fingerprint,
        "started_at": "2026-07-21T00:00:00+00:00",
    }), encoding="utf-8")


_READY_STUB = textwrap.dedent(
    """
    import hashlib, json, sys
    from pathlib import Path
    state = Path(sys.argv[1]); state.mkdir(parents=True, exist_ok=True)
    port = int(sys.argv[2])
    (state / "runtime.token").write_text("t0ken", encoding="utf-8")
    fp = hashlib.sha256(b"t0ken").hexdigest()[:12]
    (state / "runtime.json").write_text(json.dumps({
        "protocol": "eee.runtime/1", "host": "127.0.0.1", "port": port,
        "pid": 1234, "process_nonce": "nonce", "token_file": "runtime.token",
        "token_fingerprint": fp, "started_at": "2026-07-21T00:00:00+00:00",
    }), encoding="utf-8")
    """
)

_HANG_STUB = "import time; time.sleep(60)"


def _spawn_stub(command, **kwargs):
    # Replace the requested interpreter with the test interpreter running a
    # stub script that mimics backend discovery publishing.
    stub = Path(kwargs.pop("stub_path"))
    return subprocess.Popen(
        [sys.executable, str(stub), *command[1:]], **kwargs
    )


def test_probe_reports_not_running(tmp_path: Path) -> None:
    assert bl.discovery_ready(tmp_path) is False


def test_probe_reports_ready(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        _write_valid_discovery(tmp_path, port=listener.getsockname()[1])
        assert bl.discovery_ready(tmp_path) is True
    finally:
        listener.close()


def test_probe_rejects_stale_discovery(tmp_path: Path) -> None:
    # Grab a port, then release it so nothing is listening there.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    _write_valid_discovery(tmp_path, port=closed_port)
    assert bl.discovery_ready(tmp_path) is False


def test_interpreter_candidates_prefer_repo_venv(tmp_path: Path) -> None:
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    candidates = bl.interpreter_candidates(tmp_path)
    assert candidates[0] == venv_python


def test_ensure_runtime_spawns_and_waits(tmp_path: Path) -> None:
    state = tmp_path / "state"
    stub = tmp_path / "stub.py"
    stub.write_text(_READY_STUB, encoding="utf-8")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    # Hold a listener so the published endpoint is actually reachable.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]

    def spawn(command, **kwargs):
        return subprocess.Popen(
            [sys.executable, str(stub), str(state), str(port)],
            **{k: v for k, v in kwargs.items()
               if k in {"stdout", "stderr", "cwd", "env"}})

    try:
        result = bl.ensure_runtime(tmp_path, state, timeout_seconds=5.0,
                                   spawn=spawn)
        assert result.status == "spawned"
        assert result.process is not None
        result.process.wait(timeout=5)
        assert bl.discovery_ready(state) is True
    finally:
        listener.close()


def test_ensure_runtime_times_out_with_log(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    stub = tmp_path / "hang.py"
    stub.write_text(_HANG_STUB, encoding="utf-8")
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    def spawn(command, **kwargs):
        return subprocess.Popen([sys.executable, str(stub)],
                                **{k: v for k, v in kwargs.items()
                                   if k in {"stdout", "stderr", "cwd", "env"}})

    result = bl.ensure_runtime(tmp_path, state, timeout_seconds=0.3,
                               spawn=spawn)
    assert result.status == "timeout"
    assert result.log_path is not None and result.log_path.exists()
    assert result.process is not None
    bl.terminate(result.process)


def test_ensure_runtime_missing_interpreter(tmp_path: Path) -> None:
    def failing_spawn(command, **kwargs):
        raise FileNotFoundError(command[0])

    result = bl.ensure_runtime(tmp_path, tmp_path / "state",
                               timeout_seconds=0.1, spawn=failing_spawn)
    assert result.status == "missing_interpreter"


def test_terminate_is_graceful_then_force() -> None:
    proc = subprocess.Popen([sys.executable, "-c", _HANG_STUB])
    bl.terminate(proc, timeout=1.0)
    assert proc.poll() is not None
