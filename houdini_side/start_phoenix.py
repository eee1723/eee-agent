"""Launch the Phoenix tracing server (in the agent venv) from inside Houdini.

One-click via the EEE Agent menu. Idempotent: if Phoenix is already up on :6006,
just reports it. Spawns the venv python in a new console window so you can see its
logs and close the window to stop it.

Requires: ``arize-phoenix`` installed in ``.venv`` (it is **not** in the frozen
runtime lockfile — install it separately with
``uv pip install arize-phoenix openinference-instrumentation-langchain``) and
``EEE_TRACING=phoenix`` in ``.env`` for the agent to actually emit traces.
"""
from __future__ import annotations

import os
import socket
import subprocess

PHOENIX_PORT = 6006


def _venv_python():
    root = os.environ.get("EEE_PATH")
    if not root:
        return None
    p = os.path.join(root, ".venv", "Scripts", "python.exe")
    return p if os.path.exists(p) else None


def _is_up() -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", PHOENIX_PORT)) == 0
    finally:
        s.close()


def start() -> None:
    if _is_up():
        print(f"[eee] Phoenix already running at http://localhost:{PHOENIX_PORT}")
        return
    py = _venv_python()
    if not py:
        raise RuntimeError(
            "EEE_PATH not set or .venv/Scripts/python.exe not found. "
            "Run houdini_side/install_menu.py + build the venv per SETUP.md.")
    # Probe the dependency before spawning so the user gets a clear message
    # instead of a ModuleNotFoundError inside the console window. Phoenix is
    # an optional extra, not part of the frozen runtime lockfile.
    probe = subprocess.run(
        [py, "-c", "import phoenix.server.main"],
        capture_output=True)
    if probe.returncode != 0:
        raise RuntimeError(
            "Phoenix is not installed in the agent venv. Install it with:\n"
            "  uv pip install arize-phoenix "
            "openinference-instrumentation-langchain\n"
            "Phoenix is an optional observability extra, not part of the "
            "frozen runtime lockfile.")
    # New console window: user sees Phoenix logs and can close it to stop the server.
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen([py, "-m", "phoenix.server.main", "serve"], creationflags=flags)
    print(f"[eee] Phoenix starting... UI at http://localhost:{PHOENIX_PORT}")


if __name__ == "__main__":
    start()
