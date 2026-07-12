# Houdini side: start the RPC bridge

> **Fresh machine?** See [`../SETUP.md`](../SETUP.md) for the full clone→run steps
> (venv build, rpyc pin, .env, menu install). This doc covers the in-Houdini bits.

The agent process (in `.venv`) drives Houdini over an **rpyc** connection. You must
start the RPC server **inside Houdini** before the agent can do anything.

## 0. Install the menu bar entry (do once)

Run once (registers a Houdini package so the menu loads + `start_rpc`/`chat_panel`
are importable):

```
& "<houdini>\bin\hython.exe" "<repo>\houdini_side\install_menu.py"
# <houdini> e.g. C:\Program Files\Side Effects Software\Houdini 21.0.440
# <repo>     e.g. Z:\EEE_Project\EEEProceduralModeling  (your clone)
```

Then **restart Houdini**. An **EEE Agent** menu appears in the menu bar with:
- **Open Agent Panel** — starts the RPC bridge + opens the chat panel (the one-click path)
- **Start RPC Bridge Only**
- **Install / Help**

This writes `eee_agent.json` into `$HOUDINI_USER_PREF_DIR/packages/` (mirrors Edini's
package registration). The menu's items `import start_rpc, chat_panel` — made
importable by the package's `houdini.python3.11libs` entry.

---

## 1. Start the RPC server + open the panel (one line)

In Houdini, open **Windows ▸ Python Source Editor** and paste this single line:

```
exec(open(r"<repo>\houdini_side\launch.py").read())
```

This starts the localhost RPC bridge (idempotent — safe to run again) AND opens
the chat panel. You should see `[eee] Houdini RPC server listening on 127.0.0.1:18811`
and the "EEE Procedural Modeling Agent" window.

To use a different port, set `HOUDINI_RPC_PORT` in `.env` to match a custom
`start_rpc.start(port=...)` call.

---

## Manual alternative (if you prefer separate steps)

In Houdini, open **Windows ▸ Python Source Editor** and run:

```python
import sys
sys.path.insert(0, r"<repo>\houdini_side")
import start_rpc
start_rpc.start()          # listens on 127.0.0.1:18811
```

You should see in the console:

```
[eee] Houdini RPC server listening on 127.0.0.1:18811
```

To use a different port: `start_rpc.start(port=18812)` and set `HOUDINI_RPC_PORT`
in `.env` to match.

> Why not `hrpyc.start_server()`? Verified against Houdini 21.0.440's `hrpyc.py`:
> it has no `host` kwarg and binds `0.0.0.0` (all interfaces) with **no
> authentication**. `start_rpc.start()` builds the rpyc server itself and binds
> **127.0.0.1 only** (loopback) — safe on a workstation.

## 2. Make it a shelf button (optional, convenient)

1. Right-click the shelf ▸ **New Tool…**
2. **Script** tab, paste the snippet above.
3. Click the tool to (re)start the bridge anytime.

## 3. Verify from the agent side

In a terminal at the project root:

```powershell
.\.venv\Scripts\python.exe -m eee_agent.cli selftest
```

This connects, builds a box, reads its stats (expect **8 points / 6 prims**), and
exports `output\selftest_box.obj`. If it prints `[PASS]`, the whole bridge works.

## 4. Open the chat panel (Phase 3)

The panel is a thin PySide6 client that spawns the agent in `.venv` and streams
replies. It uses only Houdini's bundled PySide6 — nothing extra installed.

In Houdini's Python Source Editor (or a shelf tool):

```python
import sys
sys.path.insert(0, r"<repo>\houdini_side")
import start_rpc, chat_panel
start_rpc.start()        # ensure the bridge is up
chat_panel.open_panel()  # opens the chat window
```

Type a request (e.g. "Build a 3-storey house with windows, export to
output/house.obj") and watch the agent build it live in the viewport. The panel
spawns `.venv\Scripts\python.exe -m eee_agent.cli stdio` and talks JSON-lines.

> The agent's LLM provider/model come from `<repo>\.env`
> (`EEE_LLM_PROVIDER`, `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`).
