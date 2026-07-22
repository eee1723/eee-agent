# Workspace Hygiene and Branch Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every recoverable local artifact, make `E:\eee-agent` a clean local `main`, retain `E:\eee-agent\.worktrees\runtime` as the sole active development worktree required by the installed Houdini package, and start Stage A from the accepted documentation baseline.

**Architecture:** Workspace cleanup is a provenance-preserving migration, not a delete operation. The obsolete root branch is anchored with a local archive tag; untracked local-only files move to a dated directory outside Git; the documentation commit is integrated into local `main`; the fixed Runtime worktree is then switched to `feature/a-stability`. Every destructive-looking operation has a precondition and a postcondition.

**Tech Stack:** Git for Windows, PowerShell 7, Git worktrees, Houdini 21 package JSON.

**Workspace:** Administrative commands run from `E:\eee-agent`. Product work remains in `E:\eee-agent\.worktrees\runtime`. No remote branch, remote tag, or process is changed by this plan.

**Approved references:**

- `docs/superpowers/specs/2026-07-22-runtime-next-roadmap-design.md`
- `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`
- documentation commit `ddf7e87`
- obsolete root-only commit `bc351c0`
- accepted upstream baseline `5a6880b`

---

### Task 1: Freeze provenance and re-check stop conditions

**Files:**

- Read: `E:\eee-agent\.git`
- Read: `C:\Users\EEE\Documents\houdini21.0\packages\eee_agent.json`
- Read: all registered worktree metadata

- [ ] **Step 1: Record branch, commit, dirt, and worktree ownership**

Run from `E:\eee-agent`:

```powershell
git fetch --prune origin
git status --short --branch
git worktree list --porcelain
git rev-parse HEAD
git rev-parse origin/main
git log --oneline --decorate --left-right HEAD...origin/main
git -C .worktrees/runtime status --short --branch
git -C .worktrees/main-review-20260722 status --short --branch
```

Expected:

- root is `wip/pre-migration-main` at `bc351c0`, with only the already inventoried untracked/local files;
- `origin/main` is `5a6880b` unless a new upstream commit arrived;
- Runtime worktree is clean;
- review worktree contains only the committed documentation work;
- if upstream moved or any unexpected tracked change appears, stop and rebase the plan on the newly observed state before moving files.

- [ ] **Step 2: Verify the installed package still pins the physical Runtime worktree**

```powershell
$package = Get-Content -LiteralPath 'C:\Users\EEE\Documents\houdini21.0\packages\eee_agent.json' -Raw | ConvertFrom-Json
$package.env | ConvertTo-Json -Depth 8
Get-Process houdini* -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,MainWindowTitle
```

Expected: `EEE_PATH` resolves to `E:/eee-agent/.worktrees/runtime`. An open Houdini process is evidence that the physical path must not be removed or renamed; this plan does not terminate it.

- [ ] **Step 3: Create an immutable local anchor for the obsolete root commit**

```powershell
git show-ref --verify --quiet refs/tags/archive/pre-migration-main-2026-07-22
if ($LASTEXITCODE -ne 0) {
    git tag -a archive/pre-migration-main-2026-07-22 bc351c0 -m 'Archive pre-migration root workspace before 2026-07-22 cleanup'
}
git rev-parse archive/pre-migration-main-2026-07-22^{commit}
```

Expected: the final command prints `bc351c0...`. This is a local tag only; do not push it.

---

### Task 2: Archive root-local artifacts outside the repository

**Files:**

- Move: `E:\eee-agent\.zcode\` to `E:\eee-agent-local-archive\2026-07-22\zcode\`
- Move: `E:\eee-agent\kimi-debug-session_-20260720-064438.zip` to `E:\eee-agent-local-archive\2026-07-22\debug-archives\`
- Move: `E:\eee-agent\kimi-debug-session_-20260720-080203.zip` to `E:\eee-agent-local-archive\2026-07-22\debug-archives\`
- Move: root `uv.lock` to `E:\eee-agent-local-archive\2026-07-22\uv.lock.pre-migration` when it differs from `origin/main:uv.lock`, otherwise to `uv.lock.matches-main`
- Create: `E:\eee-agent-local-archive\2026-07-22\MANIFEST.txt`

- [ ] **Step 1: Resolve and validate every source and destination**

```powershell
$repo = (Resolve-Path -LiteralPath 'E:\eee-agent').Path
$archiveRoot = 'E:\eee-agent-local-archive\2026-07-22'
$archiveParent = Split-Path -Parent $archiveRoot
New-Item -ItemType Directory -Force -Path $archiveParent | Out-Null
$resolvedParent = (Resolve-Path -LiteralPath $archiveParent).Path
if (-not $resolvedParent.StartsWith('E:\eee-agent-local-archive', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Archive target escaped the approved root: $resolvedParent"
}
git status --short
```

Expected: source root is exactly `E:\eee-agent`; destination is under `E:\eee-agent-local-archive`; the status list matches Task 1.

- [ ] **Step 2: Create the archive directories and manifest before moving anything**

Use `New-Item` for the dated directory and its `debug-archives` child. Build `MANIFEST.txt` with these fields: source absolute path, destination relative path, file length, SHA-256, archive timestamp, root branch, root commit, upstream commit. Do not include secret file contents.

For the currently inventoried `.zcode` plan, calculate the digest with:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath 'E:\eee-agent\.zcode\plans\plan-sess_e222c96a-7608-4698-acaf-64ae0534f78f.md'
```

Before archiving, enumerate `.zcode` again with `Get-ChildItem -LiteralPath 'E:\eee-agent\.zcode' -File -Recurse` and record every relative path and digest, including any file added since planning. Abort if any source is outside `E:\eee-agent` or any destination is outside the approved archive root.

- [ ] **Step 3: Preserve the stale lock only when it is materially different**

```powershell
$rootLock = git hash-object -- uv.lock
$mainLock = git rev-parse origin/main:uv.lock
if ($rootLock -ne $mainLock) {
    Move-Item -LiteralPath 'E:\eee-agent\uv.lock' -Destination "$archiveRoot\uv.lock.pre-migration"
} else {
    Move-Item -LiteralPath 'E:\eee-agent\uv.lock' -Destination "$archiveRoot\uv.lock.matches-main"
}
```

Expected: the untracked lock is preserved outside the repository under a name that records whether it differed. This removes the untracked-path collision so Task 3 can safely switch to a branch where `uv.lock` is tracked.

- [ ] **Step 4: Move only the inventoried untracked artifacts**

Use `Move-Item -LiteralPath` for the exact `.zcode` directory and the two exact ZIP filenames listed above. Do not use wildcards and do not recursively delete anything. After moving, run:

```powershell
git status --short
Get-ChildItem -LiteralPath $archiveRoot -Recurse -File | Get-FileHash -Algorithm SHA256
```

Expected: no untracked `.zcode` or ZIP remains in the repository, archive digests match the manifest, and tracked root differences are unchanged.

---

### Task 3: Restore root as the stable local main and integrate the plans

**Files:**

- Modify Git refs only; no product files are manually edited.

- [ ] **Step 1: Require a clean root index and worktree**

```powershell
git status --porcelain=v1
```

Expected: empty. If not empty, stop; do not force checkout or reset.

- [ ] **Step 2: Switch root to local main and fast-forward to upstream**

```powershell
git switch main
git merge --ff-only origin/main
git status --short --branch
```

Expected: root is clean on `main` at the current `origin/main`. Never use `reset --hard`.

- [ ] **Step 3: Integrate the reviewed documentation commit without rewriting it**

If local `main` is still `5a6880b` and `ddf7e87` is its direct descendant:

```powershell
git merge --ff-only codex/main-review-20260722
```

If upstream advanced, create a temporary branch from the new `main`, cherry-pick `ddf7e87`, resolve documentation-only conflicts, rerun the documentation checks, and fast-forward `main` to that reviewed result. Do not merge product changes hidden inside another branch.

- [ ] **Step 4: Verify the integrated baseline**

```powershell
git status --short --branch
git log -3 --oneline --decorate
git diff --check origin/main...HEAD
rg -n --glob '2026-07-22-*' "TODO|TBD|PLACEHOLDER|fill this" docs/superpowers/specs docs/superpowers/plans docs/superpowers/reviews
```

Expected: root is clean; the only commits ahead of upstream are reviewed documentation commits; `git diff --check` passes; the placeholder scan has no accidental implementation placeholders.

---

### Task 4: Re-home the fixed Runtime worktree onto Stage A

**Files:**

- Preserve: `E:\eee-agent\.worktrees\runtime\.env`
- Preserve: `E:\eee-agent\.worktrees\runtime\.venv\`
- Modify Git branch attached to the existing physical worktree

- [ ] **Step 1: Verify ignored runtime-local state before switching**

```powershell
git -C E:\eee-agent\.worktrees\runtime status --porcelain=v1 --ignored
git -C E:\eee-agent\.worktrees\runtime check-ignore -v .env .venv
```

Expected: there are no tracked or untracked non-ignored changes; `.env` and `.venv` are ignored. Do not display `.env` contents.

- [ ] **Step 2: Create Stage A from the accepted local main in place**

```powershell
git -C E:\eee-agent\.worktrees\runtime switch -c feature/a-stability main
git -C E:\eee-agent\.worktrees\runtime status --short --branch
git -C E:\eee-agent\.worktrees\runtime merge-base --is-ancestor main HEAD
```

Expected: the physical path remains unchanged, branch is `feature/a-stability`, and it contains the accepted plans.

- [ ] **Step 3: Re-verify Houdini resolution and Python environment**

```powershell
Test-Path -LiteralPath 'E:\eee-agent\.worktrees\runtime\.venv\Scripts\python.exe'
git -C E:\eee-agent\.worktrees\runtime rev-parse HEAD
```

Expected: virtual environment exists, HEAD equals accepted local main, installed package path still points at this directory.

---

### Task 5: Remove only the temporary review worktree and close the hygiene finding

**Files:**

- Remove registered temporary worktree: `E:\eee-agent\.worktrees\main-review-20260722`
- Update: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md` in the Runtime worktree

- [ ] **Step 1: Prove the review commit is reachable and the review worktree is clean**

```powershell
git merge-base --is-ancestor ddf7e87 main
git -C E:\eee-agent\.worktrees\main-review-20260722 status --porcelain=v1
```

Expected: first command exits 0, second prints nothing.

- [ ] **Step 2: Remove the registered review worktree safely**

```powershell
$target = (Resolve-Path -LiteralPath 'E:\eee-agent\.worktrees\main-review-20260722').Path
if (-not $target.StartsWith('E:\eee-agent\.worktrees\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Worktree target escaped approved root: $target"
}
git worktree remove -- E:\eee-agent\.worktrees\main-review-20260722
git worktree prune --dry-run
git worktree list --porcelain
```

Expected: only root and the fixed Runtime worktree remain. Do not remove the Runtime worktree.

- [ ] **Step 3: Record RN-009 as resolved with evidence**

On `feature/a-stability`, change RN-009 to resolved and record: archive location, archive manifest digest, local archive tag, root/main commit, Runtime branch and path. Do not record secrets or unredacted local payloads.

- [ ] **Step 4: Commit the findings update**

```powershell
git add docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "docs: record clean runtime development baseline"
```

- [ ] **Step 5: Final hygiene verification**

```powershell
git -C E:\eee-agent status --short --branch
git -C E:\eee-agent\.worktrees\runtime status --short --branch
git worktree list
git fsck --no-reflogs --unreachable
```

Expected: root is clean local `main`; Runtime worktree is clean on `feature/a-stability`; no unexpected worktree exists; `bc351c0` remains reachable through the local archive tag. Any unrelated unreachable object is reported, not deleted.
