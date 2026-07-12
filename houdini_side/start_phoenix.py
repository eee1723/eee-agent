"""Launch the Phoenix tracing server (in the agent venv) from inside Houdini.

One-click via the EEE Agent menu. Idempotent: if Phoenix is already up on :6006,
just reports it. Spawns the venv python in a new console window so you can see its
logs and close the window to stop it.

Requires: Phoenix installed in .venv (it is, via pyproject), and EEE_TRACING=phoenix
in .env for the agent to actually emit traces.
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
    # New console window: user sees Phoenix logs and can close it to stop the server.
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen([py, "-m", "phoenix.server.main", "serve"], creationflags=flags)
    print(f"[eee] Phoenix starting... UI at http://localhost:{PHOENIX_PORT}")


if __name__ == "__main__":
    start()
