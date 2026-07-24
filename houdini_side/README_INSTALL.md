# Houdini side: Runtime control and authenticated bridge

Install the package once from the repository root:

```powershell
& "<houdini>\bin\hython.exe" "<repo>\houdini_side\install_menu.py"
```

Restart Houdini. The **EEE Agent** menu contains only:

- **Open Runtime Control** — starts the authenticated Secure Bridge and opens
  the Session/Run/approval/scene panel.
- **Start Secure Bridge Only**
- **Stop Secure Bridge**
- **Start Phoenix Tracing Server**
- **Install / Help**

Start the production Runtime from a repository terminal:

```powershell
uv run --frozen --extra eval python -m eee_agent.runtime serve
```

The Runtime panel authenticates through discovery/token files, sends bounded
commands, and exposes no direct HOM, SQLite, or raw operation JSON. Scene
changes use the **sandbox + verify + commit** workflow: the agent builds in an
isolated `/obj/eee_scratch_<run>` container, then promotes verified geometry
through four hard gates (bake / structure / orientation / health) via
`scratch_commit`. Sandbox containers are cleaned up on run end/cancel/restart.

The former unauthenticated rpyc server, chat panel, and interactive CLI are
removed. They must not be started manually or used as a rollback path.

For manual bridge lifecycle checks in Houdini's Python Source Editor:

```python
import secure_bridge_host
secure_bridge_host.start()
print(secure_bridge_host.status())
secure_bridge_host.stop()
```
