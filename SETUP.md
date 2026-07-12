# Fresh-machine setup (cross-machine development)

This gets the EEE Agent running on a new machine from a clean `git clone`. The
agent venv is machine-specific (it must match the local Houdini's bundled rpyc),
so it is `.gitignore`d and rebuilt here.

## Prereqs
- **Houdini 21** installed (any 21.x build). Note the install path, e.g.
  `C:\Program Files\Side Effects Software\Houdini 21.0.440`.
- **Git**.
- The agent runs in its own venv; Houdini's own Python is untouched.

## Steps

### 1. Clone
```
git clone <repo-url> EEEProceduralModeling
cd EEEProceduralModeling
```

### 2. Build the agent venv from Houdini's bundled Python (guarantees version match)
```
& "C:\Program Files\Side Effects Software\Houdini 21.0.440\python311\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```
(Adjust the Houdini path to your build.)

### 3. ⚠️ Pin rpyc to THIS machine's Houdini version (critical)
The venv's `rpyc` MUST equal Houdini's bundled rpyc or RPC fails
(`ValueError: invalid message type: 18`). Check + pin:
```
# check Houdini's rpyc version:
& "C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe" -c "import rpyc; print(rpyc.version.version)"
# pin the venv to that exact version (21.0.440 ships 4.1.0):
.\.venv\Scripts\python.exe -m pip install "rpyc==4.1.0"
```
If your Houdini build ships a different rpyc, use that version and update the pin
in `pyproject.toml` too.

### 4. Install the agent + deps
```
.\.venv\Scripts\python.exe -m pip install -e .
```

### 5. Configure `.env`
```
copy .env.example .env
# then edit .env: set DEEPSEEK_API_KEY (or EEE_LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY)
```
`.env` is gitignored — never commit keys.

### 6. Register the Houdini menu package (loads the "EEE Agent" menu bar entry)
```
& "C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe" houdini_side\install_menu.py
```
This writes `eee_agent.json` into `$HOUDINI_USER_PREF_DIR/packages/` so the
`MainMenuCommon.xml` loads and `start_rpc`/`chat_panel` are importable.

### 7. Restart Houdini
An **EEE Agent** menu appears. Click **Open Agent Panel** to start the RPC bridge
+ open the chat panel.

## Verify
- `.\.venv\Scripts\python.exe -m eee_agent.cli selftest` — needs the Houdini RPC
  bridge running first (EEE Agent → Start RPC Bridge). Expect `[PASS] box 8/6`.
- Optional tracing: `set EEE_TRACING=phoenix` then run; view at
  `http://localhost:6006`.
- Optional memory: `set EEE_CONTEXTSEEK=true` (file-backed, persists across runs).

## Notes
- All paths in code derive from `EEE_PATH` (set by the Houdini package) or `__file__`,
  so the repo location is portable. The only machine-specific value is the Houdini
  install path (used in the commands above) + the rpyc pin (step 3).
- See `CLAUDE.md` for the full project context + gotchas.
