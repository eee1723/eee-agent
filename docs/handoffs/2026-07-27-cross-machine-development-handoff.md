# Cross-machine development handoff — 2026-07-27

This is the current continuation entry point for
`feature/html-to-houdini-pipeline`. Older handoffs remain milestone evidence;
when their status conflicts with this file, use this file plus the current code
and test results.

## Repository state

- GitHub: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/html-to-houdini-pipeline`
- Baseline immediately before this documentation closeout:
  `5cc954a27806a748168b7093ced7fcdd61c24c61`
- The branch contains the HTML-to-Houdini validation work, scratch-native
  Provider evidence migration, task graph/node lifecycle implementation, and
  adversarial cleanup-path hardening.
- The three previously local-only guides under `docs/html/` are now tracked so
  they survive the machine transfer. They are orientation material, not
  acceptance evidence.

On the new machine, confirm the actual remote tip instead of relying on a copied
SHA:

```powershell
git status --short --branch
git log -1 --oneline --decorate
```

## Resume on a new Windows machine

```powershell
git clone https://github.com/eee1723/eee-agent.git
Set-Location eee-agent
git fetch origin
git switch --track origin/feature/html-to-houdini-pipeline
uv sync --frozen --extra eval
uv run --frozen --extra eval python -m eee_agent.cli versions
uv run --frozen --extra eval pytest -q
```

Do not copy `.venv`, Runtime SQLite files, `runtime.token`, or Houdini-local
state between machines. Houdini is an external machine dependency. Probe its
installation and use that installation's `bin\hython.exe`; the previously
verified path was:

```text
C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe
```

If the new machine uses another path, set `EEE_HFS` locally or substitute the
actual executable path in the smoke commands.

## Verified baseline

- Full offline gate: `3549 passed, 11 skipped` (includes the Windows
  RuntimeLock deflake cherry-picked from main and two new output-resolution
  tests).
- Dependency/version probe:
  `uv run --frozen --extra eval python -m eee_agent.cli versions` passed.
- Chrome HTML sketch quality smoke passed with a nonblank 1440×900 PNG.
- Scratch build/verify/commit, Bridge journey, task storage, lifecycle cleanup,
  bounded agent summary, and the Qt-free panel projection are covered by the
  offline suite.
- Real Hython task-graph smoke reached build, commit, layout, topology, and
  allowlisted deletion.
- The cleanup-path adversarial review fixed traversal and nested-target
  validation in commit `8d90cf4`.

## Open acceptance work

1. ~~Houdini display/render flags~~ **RESOLVED (2026-07-27).** The fresh-read
   failure was not a Houdini lifecycle/deferred-state issue: `scratch_exec`
   never sets the sandbox display flag, so the flag stayed on the accidental
   first-created node (`box1`), and `_scratch_output_node` preferred that
   flag holder over the chain end — commit verified and flagged the wrong
   node. Fix: `_scratch_output_node` now prefers the unique terminal sink
   (child with no downstream connections), falling back to the flag holder
   then the last child. Verified on Houdini 21.0.440 (`D:\houdini`):
   `tests/runtime/task_graph_houdini_smoke.py` prints `SMOKE OK` with no
   external reassert; `SCRATCH SMOKE OK` and `SCRATCH BRIDGE JOURNEY OK`
   regression-passed; offline gate 3549 passed, 11 skipped.

2. **Real Provider acceptance:** the scratch-native adapter is implemented, but
   the new chain has not completed the planned three real-Provider runs. Use the
   provider-specific key only in the local process environment. For the default
   DeepSeek provider that is `DEEPSEEK_API_KEY`; never commit it or paste it into
   chat. The strict harness entry point is:

   ```powershell
   $env:HFS = 'C:\Program Files\Side Effects Software\Houdini 21.0.440'
   $env:EEE_RUN_RUNTIME_MVP_PROVIDER_E2E = 'true'
   $env:EEE_RUNTIME_MVP_PROVIDER_COMMAND = 'python tests/runtime/provider_journey.py'
   uv run --frozen --extra eval python tests/runtime/runtime_mvp_provider_e2e.py
   ```

3. **Wave B:** B1 Chrome quality gate is complete; B2–B6 multi-session HTML
   cases, stability, fault injection, and feedback reports remain pending.
4. **Wave C:** C1–C7 typed expressions, components, parameter scans, case
   library, and eval metrics remain pending.
5. **Manual Houdini UI:** inspect the read-only task graph block in the docked
   panel after a real modeling run.

The detailed execution and evidence rules are in
`docs/superpowers/plans/2026-07-26-hython-agent-chain-roadmap.md`.

## Secret and local-state rules

- No API key is required to clone, install, run the offline suite, or execute
  deterministic Hython smokes.
- A real Provider key is required only for the opt-in Provider acceptance run.
- Copy `.env.example` to `.env` locally if desired; `.env` is ignored and must
  remain untracked.
- Runtime state (`app.sqlite*`, `checkpoints.sqlite*`, `runtime.token`,
  `runtime.json`, `runtime.lock`, logs) is disposable local state and must not
  be committed.
