"""One-line launcher for Houdini. In the Python Source Editor paste exactly:

    exec(open(r"<repo>/houdini_side/launch.py").read())  # <repo> = your clone; or use the EEE Agent menu

Picks up edits to chat_panel.py WITHOUT restarting Houdini: it closes any open
panel, reloads the module, and reopens — so iterating on the panel is a one-line
re-run.
"""
import importlib
import os
import sys

# Resolve houdini_side/ portably: EEE_PATH (set by the Houdini package) -> __file__
# (when run as a script) -> error. No hardcoded machine-specific path.
_here = None
_eee = os.environ.get("EEE_PATH")
if _eee:
    _here = os.path.join(_eee, "houdini_side")
if not _here or not os.path.exists(os.path.join(_here, "start_rpc.py")):
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        _here = None
if not _here or not os.path.exists(os.path.join(_here, "start_rpc.py")):
    raise RuntimeError(
        "Could not locate houdini_side/. Run houdini_side/install_menu.py "
        "(sets EEE_PATH) or invoke this file directly with python.")
if _here not in sys.path:
    sys.path.insert(0, _here)

import start_rpc  # noqa: E402
import chat_panel  # noqa: E402

# Close any panel from a previous run, then reload chat_panel so code edits apply
# without restarting Houdini. (start_rpc is left as-is: its _started flag keeps
# start() idempotent.)
_old = getattr(chat_panel, "_panel", None)
if _old is not None:
    try:
        _old.close()
    except Exception:
        pass
importlib.reload(chat_panel)

start_rpc.start()       # idempotent
chat_panel.open_panel()
