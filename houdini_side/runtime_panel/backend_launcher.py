"""Automatic Runtime backend startup for the panel (Qt-free).

Discovery contract: the backend publishes runtime.json + runtime.token into
the state dir (see eee_agent.panel.client_state.load_runtime_credentials).
The launcher probes, spawns the repo venv interpreter, waits for discovery,
and classifies the outcome for the panel's offline card. Whoever spawns,
reaps: a panel-spawned process must be passed to ``terminate`` on panel
teardown; a pre-existing backend is never killed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from eee_agent.panel.client_state import (
    PanelClientError,
    load_runtime_credentials,
)

MAX_LOG_BYTES = 65536
# Cold-start import of deepagents/langchain + DB open + bind can exceed 10s,
# especially under antivirus scanning. 20s is still snappy (the loop polls
# every 100ms so a ready backend is detected within ~0.1s of publishing).
DEFAULT_TIMEOUT_SECONDS = 20.0

SpawnFn = Callable[..., subprocess.Popen]


@dataclass(slots=True)
class LaunchResult:
    status: str  # ready|spawned|timeout|failed|missing_interpreter
    detail: str
    process: subprocess.Popen | None = None
    log_path: Path | None = None


def discovery_ready(state_dir: Path) -> bool:
    try:
        creds = load_runtime_credentials(state_dir)
    except PanelClientError:
        return False
    # Identity files can outlive a killed backend (no graceful cleanup on
    # Windows), so verify the endpoint is actually listening.
    try:
        with socket.create_connection((creds.host, creds.port), timeout=0.25):
            return True
    except OSError:
        return False


def interpreter_candidates(repo_root: Path) -> list[Path]:
    return [
        repo_root / ".venv" / "Scripts" / "python.exe",  # Windows venv
        repo_root / ".venv" / "bin" / "python",          # POSIX fallback
    ]


def build_spawn_command(python_exe: Path) -> list[str]:
    return [str(python_exe), "-m", "eee_agent.runtime", "serve"]


def _open_bounded_log(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "runtime.launch.log"
    if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
        log_path.write_bytes(b"")
    # Text mode so the backend's stderr is human-readable in the GUI gate.
    return log_path.open("ab"), log_path


def ensure_runtime(
    repo_root: Path,
    state_dir: Path,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    spawn: SpawnFn = subprocess.Popen,
) -> LaunchResult:
    """Ensure a Runtime backend is reachable; spawn one if needed.

    On a ``timeout`` result the process may still be running and must be
    passed to ``terminate`` by the caller.
    """
    if discovery_ready(state_dir):
        return LaunchResult(status="ready", detail="Runtime already running.")
    candidates = [p for p in interpreter_candidates(repo_root) if p.exists()]
    if not candidates:
        return LaunchResult(
            status="missing_interpreter",
            detail="No project virtualenv interpreter found under EEE_PATH.",
        )
    log_file, log_path = _open_bounded_log(state_dir)
    env = dict(os.environ)
    env.setdefault("EEE_RUNTIME_HOME", str(state_dir.parent))
    try:
        process = spawn(
            build_spawn_command(candidates[0]),
            stdout=log_file, stderr=log_file,
            cwd=str(repo_root), env=env,
        )
    except OSError as exc:
        log_file.close()
        return LaunchResult(
            status="missing_interpreter",
            detail=f"Failed to start the Runtime interpreter: {exc}",
            log_path=log_path,
        )
    # The child holds its own duplicated handle; drop the parent's copy.
    log_file.close()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if discovery_ready(state_dir):
            return LaunchResult(status="spawned",
                                detail="Runtime started by the panel.",
                                process=process, log_path=log_path)
        if process.poll() is not None:
            return LaunchResult(
                status="failed",
                detail=f"Runtime exited with code {process.returncode}.",
                process=process, log_path=log_path,
            )
        time.sleep(0.1)
    # The poll interval leaves a gap between the last check and the deadline;
    # do one final discovery check before declaring timeout. This recovers the
    # common slow-cold-start case where the backend published discovery in that
    # gap (the process is still alive and listening).
    if process.poll() is None and discovery_ready(state_dir):
        return LaunchResult(status="spawned",
                            detail="Runtime started by the panel.",
                            process=process, log_path=log_path)
    return LaunchResult(
        status="timeout",
        detail="Runtime did not publish discovery before the deadline.",
        process=process, log_path=log_path,
    )


def terminate(process: subprocess.Popen, *, timeout: float = 5.0) -> None:
    """Gracefully stop a panel-spawned backend, then force if needed."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)
