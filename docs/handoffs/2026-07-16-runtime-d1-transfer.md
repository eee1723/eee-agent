# Runtime D1 Cross-Computer Handoff - 2026-07-16

> Historical handoff: the current cross-computer source of truth is
> `docs/handoffs/2026-07-16-runtime-b2b3-transfer.md`. Keep this file for the
> accepted D1 history and evidence; do not use its old B2b resume state.

## Start Here

This is the historical D1 short-form handoff. It previously superseded the
restore instructions and resume prompt in `2026-07-15-runtime-migration.md`;
that file remains the detailed Tasks 10-16 history. Tasks 1-9 remain documented
in `2026-07-14-runtime-migration.md`. Use the B2b-3 handoff named above for a
new-computer restore.

Repository and branch:

- GitHub: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/runtime`
- Keep `feature/runtime` separate from `main` until the user makes a distinct
  integration decision.
- At handoff completion, the local branch is pushed normally to
  `origin/feature/runtime`; no force-push or merge is performed.

## Accepted Tip Chain

The newest accepted implementation and status commits are:

```text
39f7346 feat: support ordered created changeset references
2ee9a2e docs: record accepted created-reference gate
```

The transfer-document refresh is committed after `2ee9a2e` and is the remote
branch tip to fetch. Verify the fetched history contains both hashes above and
this file before starting new work.

Milestone state when this D1 handoff was originally written:

| Slice | State | Accepted tip/evidence |
| --- | --- | --- |
| Tasks 1-13 Runtime v1 | Complete, Codex accepted | Through `c8e6af1` |
| Task 14 external delivery | Restore gate passed; manual GLM/read-only continuity checks remain separate | Runtime plan |
| Task 15-A/B/C/D Secure read-only Bridge | Complete, Codex accepted | Through `bfc00f3` |
| Task 16-A contracts/policy | Complete, Codex accepted | `79f281d` |
| Task 16-B1 persistence | Complete, Codex accepted | `54f2989` |
| Task 16-B2a approvals | Complete, Codex accepted | `7b3bff8` |
| Task 16-B2b workspace lifecycle | Historical snapshot: design/plan accepted; implementation later completed through B2b-3 | `e135088`, `28b5dd9`; current tip `b2a1b80` |
| Task 16-C preflight | Complete, Codex accepted | `6050a00` |
| Task 16-D transactional apply | Complete, Codex accepted | `3435f4b` |
| Task 16-D1 ordered created refs | Complete, Codex accepted | `39f7346` |
| Task 16-E Runtime apply/recovery | Not started; requires a new bounded plan | Task 16 plan |
| Task 17 UI | Not started in this branch slice | Roadmap |

Do not begin 16-E or UI work from a D/D1 prompt. B2b uses its own accepted
design, executable plan, prompts, and review gate. A bounded slice may be
implemented directly by Codex. Claude Code is an optional implementation
worker only when the user explicitly selects it and quota is available.

## D1 Accepted Behavior

Task 16-D1 safely supports references to nodes created earlier in the same
ordered ChangeSet:

- create then set a created node parameter;
- create then connect with a created target and/or source;
- create a child under an earlier-created parent;
- repeat parm/wire writes with first-state preflight and per-write JIT stale
  checks;
- reject forward refs, cycles, contradictory created identity, impossible
  created-state preconditions/checkpoints, and stale JIT facts;
- preserve reverse rollback, truthful receipts, idempotent replay, and write
  freeze after uncertain recovery.

D1 adds no DTO tag, RPC, effect, dependency, Runtime orchestration, workspace
public command, UI surface, arbitrary code, or forward deletion.

Authoritative D1 documents:

1. `docs/superpowers/specs/2026-07-16-task16-d1-created-reference-design.md`
2. `docs/superpowers/plans/2026-07-16-task16-d1-created-references.md`
3. `docs/superpowers/reviews/2026-07-16-task16-d1-review-result.md`

## Acceptance Baseline

The final independent acceptance on 2026-07-16 was:

```text
Focused Task 16 gate:  513 passed
Full offline suite:     1872 passed, 1 skipped
uv lock --check:        69 packages, exit 0
compileall:             exit 0
git diff --check:       exit 0
Houdini 21.0.440 D1:   Applied, AlreadyApplied replay, cleanup, exit 0
```

The only skip was the existing optional WSL environment probe. No new skip or
xfail was introduced.

Exact Machine A Houdini smoke command:

```powershell
& 'C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe' tests\runtime\changeset_houdini_smoke.py
```

It created a SOP subnet, box, and xform in one ChangeSet, set the created xform,
connected the two created endpoints, queried the receipt, replayed with
`AlreadyApplied`, and destroyed `/obj/eee_task16d_smoke` without saving. The Qt
timer warning after cleanup was non-fatal and the process exited zero.

Machine B has used `D:\houdini\bin\hython.exe`. Detect the actual installation
on the new computer rather than assuming either path.

## Fresh Clone Procedure

Use Python 3.11. Do not copy `.venv` or any Runtime/Houdini local state.

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git fetch --all --prune
git switch --track origin/feature/runtime
git log -5 --oneline
uv sync --frozen --extra eval --python 3.11
uv lock --check
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

Expected result before development:

- branch is `feature/runtime` and tracks `origin/feature/runtime`;
- history includes `39f7346` and `2ee9a2e`;
- worktree is clean;
- lock check resolves/checks 69 packages;
- full suite has no failure and normally reports `1872 passed, 1 skipped` on
  Machine A; the optional WSL probe may pass rather than skip elsewhere;
- compileall and diff check exit zero.

For an existing clone:

```powershell
git fetch origin --prune
git switch feature/runtime
git pull --ff-only origin feature/runtime
uv sync --frozen --extra eval --python 3.11
```

Never use a force pull/reset to hide local changes. Inspect and preserve any
existing worktree changes before updating.

## Machine-Local State

Git intentionally does not transfer:

- `.env`, API keys, or provider credentials;
- `.venv/` and `.worktrees/`;
- Runtime `app.sqlite*` and `checkpoints.sqlite*`;
- `runtime.token`, `runtime.json`, and `runtime.lock`;
- `bridge.token` and `bridge.discovery.json`;
- logs, caches, discovery files, HIP files, or Houdini preferences.

Recreate `.env` from `.env.example` and transfer credentials only through an
encrypted secret channel. Never commit or paste secrets into source, tests,
documentation, issues, or prompts.

## Required Reading

Read in this order after the clone verifies clean:

1. `CLAUDE.md`
2. this file
3. `docs/handoffs/2026-07-15-runtime-migration.md` for detailed history
4. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`
5. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`
6. `docs/superpowers/reviews/2026-07-16-task16-d1-review-result.md`
7. `docs/superpowers/plans/2026-07-15-runtime-next-milestones.md`

## Resume Prompt

This historical prompt is intentionally retired. Use the current prompt in
`docs/handoffs/2026-07-16-runtime-b2b3-transfer.md`; do not resume from the
pre-B2b state below.

```text
Retired: see docs/handoffs/2026-07-16-runtime-b2b3-transfer.md.
```

## Git Boundary

- Push only `feature/runtime` with a normal fast-forward push.
- Do not force-push, rebase accepted history, or merge `main` here.
- Do not delete other branches/worktrees.
- A push transfers committed files only, not ignored credentials or Runtime
  state.
