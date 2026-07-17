# Fresh-Machine Setup

This guide restores EEE Agent from a clean clone on a Windows development
computer. For the exact current branch, accepted commits, verification
baseline, and resume prompt, read:

```text
docs/handoffs/2026-07-17-cross-machine-modeling-handoff.md
```

## Prerequisites

- Git
- PowerShell
- `uv`
- SideFX Houdini 21.0.440
- access to `https://github.com/eee1723/eee-agent.git`
- model-provider credentials only if you intend to run a live LLM

The repository environment is machine-specific. Do not copy `.venv` from
another computer.

## 1. Clone and Select the Development Branch

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git fetch --all --prune
git switch --track origin/feature/runtime
git status --short --branch
git log -8 --oneline
```

Expected:

- branch: `feature/runtime`
- upstream: `origin/feature/runtime`
- clean worktree
- history contains accepted modeling implementation tip `2433966` and the
  newer cross-machine handoff commit

Do not merge `main`, rebase accepted history, force-push, or use
`git reset --hard` as part of setup.

## 2. Detect the Local Houdini Installation

The two known installations are:

- `C:\Program Files\Side Effects Software\Houdini 21.0.440`
- `D:\houdini`

Detect the current machine rather than assuming:

```powershell
$houdiniCandidates = @(
    'C:\Program Files\Side Effects Software\Houdini 21.0.440',
    'D:\houdini'
)
$HFS = $houdiniCandidates |
    Where-Object { Test-Path (Join-Path $_ 'bin\hython.exe') } |
    Select-Object -First 1
if (-not $HFS) {
    throw 'Houdini 21.0.440 was not found in a known location.'
}
$HFS
```

Verify its Python and bundled rpyc:

```powershell
& (Join-Path $HFS 'bin\hython.exe') -c "import sys, rpyc; print(sys.version); print(rpyc.version.version)"
```

For Houdini 21.0.440, the expected major/minor Python is 3.11 and expected
rpyc is 4.1.0. The project's `uv.lock` pins rpyc 4.1.0 because a Bridge client
using a different rpyc wire version can fail with:

```text
ValueError: invalid message type: 18
```

If Houdini is upgraded, verify the bundled versions before changing the lock.

## 3. Rebuild the Locked Python Environment

Install `uv` if necessary, then let it create/synchronize `.venv` from
Houdini's bundled Python:

```powershell
$HoudiniPython = Join-Path $HFS 'python311\python.exe'
uv sync --frozen --extra eval --python $HoudiniPython
uv lock --check
```

Do not install the project ad hoc with `pip install -e .` on top of a divergent
environment. The checked-in lock is the dependency authority for this branch.

## 4. Restore Local Configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` locally. The default provider is documented in `CLAUDE.md`.

Important:

- `.env` is ignored by Git.
- Never commit API keys.
- Transfer secrets through a password manager or another encrypted channel.
- Claude is optional. The current development preference is direct Codex
  implementation, so Claude credentials are not required unless the user
  explicitly selects Claude for a later task.

## 5. Verify the Clean Restore

Run before editing source:

```powershell
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

The accepted Task 16-B2b baseline was 2018 offline tests passing. Later commits
may legitimately increase the count, but setup is blocked by any failure or
unexplained new skip/xfail.

For the focused Workspace gate:

```powershell
uv run --extra eval pytest `
  tests/runtime/test_protocol.py `
  tests/runtime/test_server.py `
  tests/runtime/test_service.py `
  tests/runtime/test_runtime_cli.py `
  tests/runtime/test_runtime_e2e.py `
  tests/runtime/test_workspace_service.py `
  tests/runtime/test_workspace_bridge_contracts.py `
  tests/runtime/test_workspace_bridge_inspector.py -q
```

## 6. Install the Houdini Menu Package

The installer derives the repository location and writes the package
registration into the current Houdini user preference directory:

```powershell
& (Join-Path $HFS 'bin\hython.exe') houdini_side\install_menu.py
```

Restart Houdini afterward. The **EEE Agent** menu should appear.

Only the authenticated Secure Bridge and persistent Runtime are available
through that menu. The former rpyc Bridge and chat panel entrypoints have been
removed; follow the current handoff and Runtime test harness.

## 7. Verify the Runtime CLI

The compatibility CLI intentionally exposes only the dependency report:

```powershell
uv run --extra eval python -m eee_agent.cli versions
```

## 8. Start the Persistent Runtime

```powershell
uv run --extra eval python -m eee_agent.runtime serve
```

Runtime:

- binds to `127.0.0.1`;
- uses authenticated discovery;
- stores machine-local databases and identity under
  `%LOCALAPPDATA%\EEEAgent\` unless `EEE_RUNTIME_HOME` is set;
- uses identity/token files separate from the Secure Bridge.

Do not copy Runtime or Bridge databases, bearer tokens, discovery JSON, lock
files, or checkpoints between computers.

## 9. Optional Real-Houdini Acceptance

After the full offline restore succeeds:

```powershell
& (Join-Path $HFS 'bin\hython.exe') -u tests\runtime\changeset_houdini_smoke.py
```

Expected success markers:

```text
B2B SMOKE OK
SMOKE OK
```

The smoke creates a disposable unsaved root, exercises typed ChangeSet and
Workspace behavior, and cleans the root before exit. Run it in a fresh process,
not inside a valuable production HIP session.

Houdini 21.0.440 may print this non-fatal Qt warning after successful
assertions:

```text
QObject::startTimer: Timers can only be used with threads started with QThread
```

Judge success by the assertions, cleanup markers, and process exit code.

## Machine-Local Files

Git intentionally does not transfer:

- `.env` and credentials;
- `.venv/` and `.worktrees/`;
- Runtime SQLite and checkpoint databases;
- Runtime/Bridge token, discovery, lock, and stop-marker files;
- logs, caches, coverage, and temporary diagnostics;
- HIP files, external HDAs, Houdini preferences, and UI layout.

Source code and handoff documents transfer through Git. Operational state does
not.

## Before Continuing Development

Read:

1. `CLAUDE.md`
2. `docs/handoffs/2026-07-16-runtime-b2b3-transfer.md`
3. `docs/superpowers/reviews/2026-07-16-task16-b2b-review-result.md`

No post-B2b implementation slice is automatically authorized. Task 16-E and
Task 17 require a new bounded design and plan before source changes.
