# Runtime B2b-3 Cross-Computer Handoff - 2026-07-16

## Start Here

This is the current source of truth for moving EEE Agent Runtime development
to another computer. It supersedes the restore instructions and resume prompt
in `2026-07-16-runtime-d1-transfer.md`; that file remains useful as detailed D1
history.

Repository and branch:

- GitHub: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/runtime`
- Accepted B2b implementation tip: `b2a1b80`
- Keep `feature/runtime` separate from `main` until the user makes a distinct
  integration decision.
- The handoff is intended to be pushed normally to
  `origin/feature/runtime`. Do not force-push, rebase accepted history, or
  merge `main` as part of restoring the new computer.

## Current Milestone State

| Slice | State | Accepted tip/evidence |
| --- | --- | --- |
| Tasks 1-13 Runtime v1 | Complete, Codex accepted | Through `c8e6af1` |
| Task 14 external delivery | Restore gate passed; manual GLM/read-only continuity checks remain separate | Runtime plan |
| Task 15-A/B/C/D Secure read-only Bridge | Complete, Codex accepted | Through `bfc00f3` |
| Task 16-A contracts/policy | Complete, Codex accepted | `79f281d` |
| Task 16-B1 persistence | Complete, Codex accepted | `54f2989` |
| Task 16-B2a approvals | Complete, Codex accepted | `7b3bff8` |
| Task 16-B2b trusted Workspace lifecycle | Complete, Codex accepted | `17d9371` through `b2a1b80` |
| Task 16-C preflight | Complete, Codex accepted | `6050a00` |
| Task 16-D transactional Apply | Complete, Codex accepted | `3435f4b` |
| Task 16-D1 ordered created references | Complete, Codex accepted | `39f7346` |
| Task 16-E Runtime Apply/recovery | Not started; requires a new bounded design/plan | Task 16 plan |
| Task 17 UI | Not started in this branch slice | Roadmap |

Do not infer authority to start Task 16-E or UI work from this handoff. The next
slice needs its own design decision, executable plan, file scope, tests, and
review gate.

Codex may implement a bounded slice directly. Claude Code is an optional
implementation worker only when the user explicitly selects it and has quota.
The current user preference is direct Codex implementation without Claude.

## Accepted Tip Chain

The newest accepted Workspace commits are:

```text
17d9371 feat: inspect trusted houdini workspaces
c9b2047 feat: persist trusted workspace lifecycle
776e42b feat: expose trusted workspace lifecycle
b2a1b80 test: harden workspace lifecycle acceptance
```

After fetching on the new computer, verify that history contains `b2a1b80` and
this handoff document before changing files.

## What B2b-3 Delivers

The Runtime now exposes exactly:

```text
workspace.create
workspace.bind
workspace.switch
workspace.inspect
```

In plain language:

- Workspace answers “which trusted Houdini scene and EEE-owned node set are we
  working with?”
- The user's explicit request answers “what should be done?”
- Current node selection is supporting context, not absolute authority.
- EEE identity mirrors prove that a node came from the trusted typed ChangeSet
  path. Ordinary nodes are not silently adopted.
- Workspace state does not itself permit writes. Typed policy, approval,
  preflight, the single FIFO, transactional Apply, receipt, and recovery remain
  mandatory.

Important behavior:

- `workspace.create` registers exactly the selected nodes that already contain
  all six EEE ownership mirrors. It creates or modifies no Houdini node.
- `workspace.bind` requires exactly the stored stable node-ID set and may
  refresh paths, parent paths, Bridge instance ID, and scene epoch only.
- `workspace.switch` ignores selection, re-inspects the complete stored
  manifest, and changes the active pointer only after a healthy live proof.
- `workspace.inspect` is read-only and reports `Healthy`, `Stale`, `Conflict`,
  or `BridgeUnavailable`.
- Missing Bridge facts fail create/bind/switch closed. Inspect may return
  cached persisted context with `BridgeUnavailable`.
- A stale or identity-conflict switch preserves the previous active Workspace,
  preserves `state_revision`, and appends no event.
- Exact no-ops append no event.
- Persistence changes and durable events commit atomically; notifications run
  only after commit.

## Where to Read the B2b Design

Read these in order when working on Workspace behavior:

1. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
2. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`
3. `docs/superpowers/reviews/2026-07-16-task16-b2b-review-checklist.md`
4. `docs/superpowers/reviews/2026-07-16-task16-b2b-review-result.md`

Production entry points:

- `eee_agent/houdini_bridge/workspaces.py`
- `houdini_side/workspace_inspector.py`
- `eee_agent/houdini_bridge/workspace_provider.py`
- `eee_agent/changesets/workspace_service.py`
- `eee_agent/changesets/repository.py`
- `eee_agent/runtime/protocol.py`
- `eee_agent/runtime/server.py`
- `eee_agent/runtime/service.py`
- `eee_agent/runtime/__main__.py`

Process and real-Houdini acceptance:

- `tests/runtime/test_runtime_e2e.py`
- `tests/runtime/runtime_process_fixture.py`
- `tests/runtime/changeset_houdini_smoke.py`

## Acceptance Baseline

The accepted B2b code produced:

```text
Runtime process E2E:       9 passed
B2b-3 focused gate:        325 passed
Cross-slice regression:    621 passed
Full offline suite:        2018 passed
uv lock --check:           69 packages, exit 0
compileall:                exit 0
git diff --check:          exit 0
New skip/xfail:            none
```

The final real-Houdini acceptance ran on Machine B:

```powershell
& 'D:\houdini\bin\hython.exe' -u tests\runtime\changeset_houdini_smoke.py
```

It exited zero in approximately 7.2 seconds, printed `B2B SMOKE OK` and
`SMOKE OK`, and removed `/obj/eee_task16d_smoke_bound` without saving.

Houdini printed this non-fatal warning after the assertions:

```text
QObject::startTimer: Timers can only be used with threads started with QThread
```

The warning did not affect the exit code, assertions, process cleanup, or
disposable-node cleanup.

## Fresh Clone on the New Computer

Do not copy `.venv`, `.worktrees`, Runtime state, Bridge state, or Houdini
preferences from the old computer.

Choose a local destination, then run:

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git fetch --all --prune
git switch --track origin/feature/runtime
git status --short --branch
git log -8 --oneline
```

Expected:

- current branch is `feature/runtime`;
- it tracks `origin/feature/runtime`;
- history contains `b2a1b80`;
- this file exists;
- the worktree is clean.

If the branch was created automatically by clone because it became the remote
default, use:

```powershell
git switch feature/runtime
```

If `git switch --track origin/feature/runtime` says the local branch already
exists, use the existing-clone procedure below rather than deleting anything.

## Existing Clone on the New Computer

First inspect and preserve local work:

```powershell
git status --short --branch
git branch -vv
```

If the target worktree is clean:

```powershell
git fetch origin --prune
git switch feature/runtime
git pull --ff-only origin feature/runtime
```

Never use `git reset --hard`, force pull, rebase, or branch deletion to hide
local changes. If the new computer already has uncommitted work, stop and
inspect it before updating.

## Detect Houdini and Rebuild the Environment

The two known installs are:

- Machine A:
  `C:\Program Files\Side Effects Software\Houdini 21.0.440`
- Machine B: `D:\houdini`

Do not assume the path. Check which executable exists:

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
& (Join-Path $HFS 'bin\hython.exe') -c "import sys, rpyc; print(sys.version); print(rpyc.version.version)"
```

For Houdini 21.0.440, the bundled Python is 3.11 and bundled rpyc is 4.1.0.
`uv.lock` pins the matching rpyc version.

Install `uv` if it is not already available, then rebuild from the lock using
Houdini's bundled Python:

```powershell
$HoudiniPython = Join-Path $HFS 'python311\python.exe'
uv sync --frozen --extra eval --python $HoudiniPython
uv lock --check
```

Do not copy the old `.venv`. If the new machine has a different Houdini build,
verify its bundled Python and rpyc before changing dependency pins.

## Restore Secrets Safely

Machine-local secrets are intentionally absent from Git:

```powershell
Copy-Item .env.example .env
```

Then edit `.env` locally. Transfer API keys only through an encrypted password
manager or another secret channel.

Never place credentials in:

- Git commits;
- documentation;
- prompts or chat transcripts;
- issue/PR text;
- test fixtures;
- Runtime/Bridge discovery JSON.

Claude is optional. With the current preference, no Claude credential is
required for development. Configure only the model provider actually needed
for manual agent runs.

## Clean-Restore Verification

Before development, run:

```powershell
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

The exact pass count can increase in later commits, but there must be no test
failure and no unexplained new skip/xfail. The accepted B2b baseline was 2018
offline tests passing.

For the B2b-focused restore gate:

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

## Optional Real-Houdini Restore Gate

Run this only after the offline restore is clean:

```powershell
& (Join-Path $HFS 'bin\hython.exe') -u tests\runtime\changeset_houdini_smoke.py
```

Expected successful markers:

```text
B2B SMOKE OK
SMOKE OK
```

The smoke uses a disposable unsaved root and must clean it before exit. Do not
point it at a valuable production HIP session.

## Machine-Local State That Git Does Not Transfer

The following are intentionally ignored or external:

- `.env`, API keys, and provider credentials;
- `.venv/` and `.worktrees/`;
- Runtime `app.sqlite*` and `checkpoints.sqlite*`;
- `runtime.token`, `runtime.json`, and `runtime.lock`;
- `bridge.token` and `bridge.discovery.json`;
- Runtime/Bridge stop markers and process-local discovery files;
- logs, caches, coverage, temporary smoke directories, and diagnostic files;
- HIP files, HDAs outside the repository, Houdini preferences, menu/package
  installation, and UI layout.

Runtime data is machine-local by design. A clean clone starts with no previous
Sessions, Runs, Workspace manifests, active Workspace pointer, checkpoints, or
event replay database. Source history transfers; local operational state does
not.

## Start Runtime and Bridge

Start the authenticated Secure Bridge from Houdini using the project install
instructions, then start Runtime separately:

```powershell
uv run --extra eval python -m eee_agent.runtime serve
```

Runtime and Bridge use separate bearer tokens and discovery files even when
they share the same state directory. Do not copy or merge one identity into
the other.

The legacy CLI remains available as a rollback path:

```powershell
uv run --extra eval python -m eee_agent.cli selftest
uv run --extra eval python -m eee_agent.cli versions
```

## Known Technical Notes

- Houdini 21.0.440's `haio` loop is not used to host
  `RuntimeWebSocketServer`. Acceptance runs Bridge/HOM inside `hython` and runs
  Runtime in a separate standard Python subprocess.
- The acceptance harness includes a bounded graceful shutdown and a forced
  process-tree cleanup path for a deliberately hanging subprocess.
- Workspace control JSON in process tests is atomically published to prevent
  partial cross-process reads.
- A few failed diagnostic runs may leave disposable files under the operating
  system temporary directory. They are not part of the repository or Runtime
  state. Confirm no associated process is alive before deleting any such file.
- The Qt timer warning shown in the real smoke is currently non-blocking.

## Required Reading on the New Computer

Read in this order:

1. `CLAUDE.md`
2. this file
3. `docs/superpowers/reviews/2026-07-16-task16-b2b-review-result.md`
4. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
5. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`
6. `docs/handoffs/2026-07-16-runtime-d1-transfer.md` for D1 history
7. `docs/handoffs/2026-07-15-runtime-migration.md` for Tasks 10-16 history
8. `docs/superpowers/plans/2026-07-15-runtime-next-milestones.md`

## Next Development Decision

No implementation slice is currently authorized after B2b-3.

The likely next architectural milestone is Task 16-E Runtime Apply/recovery,
but it must begin with a bounded design and implementation plan. Before
touching code:

1. Re-read the accepted Task 16 typed ChangeSet and Workspace boundaries.
2. Define the exact Runtime command surface and recovery state machine.
3. Keep Workspace as context, not permission.
4. Preserve approval, preflight, transactional Apply, receipts, event
   ordering, failure truthfulness, and the single main-thread FIFO.
5. Define process, restart, cancellation, uncertain-outcome, and real Houdini
   acceptance before implementation.
6. Confirm with the user whether Codex should implement directly. Claude
   remains optional and is currently not selected.

Task 17 UI should not start as a side effect of Task 16-E planning.

## Resume Prompt

Use this prompt at the beginning of the new-computer session:

```text
Resume EEE Agent development from origin/feature/runtime. Read CLAUDE.md and
docs/handoffs/2026-07-16-runtime-b2b3-transfer.md first. Verify the branch,
locked environment, full offline suite, compileall, and clean worktree before
changing files.

Task 16-B2b is Codex-accepted through b2a1b80. It activates exactly
workspace.create, workspace.bind, workspace.switch, and workspace.inspect.
Workspace supplies trusted scene context but never write authority. Explicit
user intent outranks selection; only nodes already carrying all six
EEE-executor ownership mirrors can enter a Workspace. Typed policy, approval,
preflight, the single FIFO, transactional Apply, receipts, rollback, and
recovery remain mandatory.

Task 16-D1 is accepted at 39f7346. Task 16-E and Task 17 have not started and
are not authorized by this prompt. Create a bounded design/plan before the next
slice. Codex should implement directly unless I explicitly choose Claude and
have quota.

Do not merge main, force-push, rebase accepted history, copy machine-local
Runtime/Bridge identity or databases, add generic Houdini execution/write
surfaces, or weaken the accepted Workspace and typed ChangeSet boundaries.
```

## Git Boundary

- Push only `feature/runtime` with a normal fast-forward push.
- Do not force-push, rebase accepted history, or merge `main`.
- Do not delete other branches or worktrees.
- A push transfers committed files only, not ignored credentials, databases,
  tokens, discovery state, logs, Houdini preferences, or `.venv`.
