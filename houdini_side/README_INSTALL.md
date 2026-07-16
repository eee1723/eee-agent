# Houdini side: Runtime observer and bridges

> Fresh machine? See [`../SETUP.md`](../SETUP.md) for the full clone-to-run
> sequence. This document covers the in-Houdini integration.

The production Runtime path uses an authenticated Secure Bridge and a dockable
read-only Python Panel. The legacy chat panel still uses the old rpyc bridge and
remains available as a rollback path.

## 0. Install the Houdini package

Run once:

```powershell
& "<houdini>\bin\hython.exe" "<repo>\houdini_side\install_menu.py"
```

Examples:

```text
<houdini> = C:\Program Files\Side Effects Software\Houdini 21.0.440
<repo>    = Z:\EEE_Project\EEEProceduralModeling
```

Restart Houdini. The **EEE Agent** menu contains:

- **Open Runtime Control** — starts the authenticated Secure Bridge and opens
  the dockable Session/Run/approval/scene panel
- **Start Secure Bridge Only**
- **Stop Secure Bridge**
- **Open Agent Panel** — legacy path: starts rpyc and opens the old chat panel
- **Start RPC Bridge Only**
- **Start Phoenix Tracing Server**
- **Install / Help**

The installer writes `eee_agent.json` into
`$HOUDINI_USER_PREF_DIR/packages/`. The package adds the repository to
`HOUDINI_PATH`, so `python_panels/EEEAgentRuntime.pypanel` appears in Houdini's
Python Panel interface menu. Houdini-side modules are also added through
`houdini.python3.11libs`.

## 1. Start the Runtime observer

Start Runtime in a terminal at the repository root:

```powershell
uv run --frozen --extra eval python -m eee_agent.runtime serve
```

Then choose **EEE Agent → Open Runtime Control** in Houdini.

The Runtime control panel:

- authenticates to Runtime through `runtime.json` and `runtime.token`;
- reconnects with the remembered per-Session `last_seq`;
- creates/selects Sessions and starts/stops bounded Runtime Runs;
- restores Run status and output from snapshots plus live events;
- displays bounded ChangeSet risk, approval, receipt, and recovery evidence;
- sends only exact `changeset.approve` or `changeset.reject` decisions for
  trusted proposals;
- reads selection through typed `workspace.inspect`, then a bound
  `scene.query`;
- displays HIP, Houdini instance, scene epoch, revision, node path/type, lock
  state, and bounded geometry statistics;
- does not expose `changeset.apply`, operation JSON, parameter values, direct
  HOM, SQLite, or an unrestricted Houdini write route.

The Secure Bridge:

- binds an ephemeral `127.0.0.1` port;
- uses its own independent `bridge.token`;
- runs transport I/O on one background asyncio thread;
- pumps every typed Houdini operation through the accepted single main-thread
  FIFO;
- remains running when the panel closes.

You can also create a Python Panel pane and select **EEE Runtime**. If the
Secure Bridge is not running, Runtime status remains available and the
selection area reports that inspection is unavailable.

## 2. Manual Secure Bridge controls

From Houdini's Python Source Editor:

```python
import secure_bridge_host

secure_bridge_host.start()
print(secure_bridge_host.status())
secure_bridge_host.stop()
```

`start()` and `stop()` are idempotent. The full token is never printed by
`status()`.

## 3. Legacy RPC/chat path

The legacy path is preserved for rollback and existing CLI workflows.

### Start RPC and open the legacy chat panel

In Houdini's Python Source Editor:

```python
exec(open(r"<repo>\houdini_side\launch.py").read())
```

This starts the localhost rpyc bridge and opens the old chat panel.

Manual equivalent:

```python
import sys
sys.path.insert(0, r"<repo>\houdini_side")
import start_rpc, chat_panel

start_rpc.start()
chat_panel.open_panel()
```

The rpyc bridge binds `127.0.0.1:18811`. Do not replace it with
`hrpyc.start_server()`: Houdini 21.0.440's helper binds `0.0.0.0` without
authentication.

### Verify the legacy bridge

```powershell
uv run --frozen --extra eval python -m eee_agent.cli selftest
```

The self-test creates and exports a disposable box, so it is not part of the
read-only Runtime observer acceptance.
