"""Start a localhost-only rpyc server inside Houdini for the external agent.

HOW TO RUN (inside Houdini):
  Open the Python Source Editor (Windows > Python Source Editor) OR point a
  shelf tool at this file, then execute:

      import sys, os
      # adjust path to your repo
      sys.path.insert(0, r"<repo>/houdini_side")  # <repo> = your clone path
      import start_rpc
      start_rpc.start()

WHY NOT hrpyc.start_server()?
  Verified against Houdini 21.0.440's hrpyc.py: start_server(port, use_thread,
  quiet) has no host kwarg and its internal --host defaults to 0.0.0.0 (all
  interfaces). rpyc has NO authentication, so binding 0.0.0.0 is unsafe. We build
  the ThreadedServer ourselves and bind 127.0.0.1 (loopback only).
"""
from __future__ import annotations

import threading

from rpyc.core import SlaveService
from rpyc.utils.server import ThreadedServer

DEFAULT_PORT = 18811

# Idempotency guard so repeated calls (e.g. pressing the shelf button twice) don't
# try to rebind the port.
_started = False


def start(port: int = DEFAULT_PORT):
    global _started
    if _started:
        print(f"[eee] RPC server already running on 127.0.0.1:{port}")
        return None
    try:
        srv = ThreadedServer(
            SlaveService,
            hostname="127.0.0.1",
            port=port,
            reuse_addr=True,
            authenticator=None,
            registrar=None,
            auto_register=False,
        )
    except OSError as e:
        print(f"[eee] could not bind 127.0.0.1:{port}: {e}. "
              "Is another bridge already running?")
        return None
    srv.logger.quiet = True
    t = threading.Thread(target=srv.start, name="eee-rpc-server", daemon=True)
    t.start()
    _started = True
    print(f"[eee] Houdini RPC server listening on 127.0.0.1:{port}")
    return srv


if __name__ == "__main__":
    start()
