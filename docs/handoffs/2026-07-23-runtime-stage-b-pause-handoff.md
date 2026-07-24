# Runtime Stage B Pause Handoff

> **SUPERSEDED (2026-07-24).** This pause handoff is historical. Stage B
> acceptance later recorded B-07 (interactive GUI checklist) and B-08
> (Vision real-provider journey) as PASS and merged to `main`. The current
> entry doc is `docs/handoffs/2026-07-24-sandbox-verify-commit-handoff.md`.


Date: 2026-07-23
Owner on pause: Codex
Working directory: `Z:/EEE_Project/EEEProceduralModeling/.worktrees/runtime`
Branch: `feature/b-release-acceptance`
Remote: `origin` (`https://github.com/eee1723/eee-agent.git`)

## Pause point

Development is intentionally paused after Stage B's offline and Houdini
command-line acceptance gates. Before this handoff commit, the exact branch tip
was:

```text
c4a2dd9 docs: record stage B offline acceptance
```

The branch is based on the accepted Stage A evidence commit `4db806d`. It has
not been merged or rebased onto `main`, no release tag has been created, and
Stage C has not started. The current Stage B decision remains **NOT READY**.

## Current fact matrix

| Fact surface | Status | Evidence / boundary |
| --- | --- | --- |
| Code | verified-current | Stage A accepted; Stage B Vision configuration, failure semantics, production wiring, strict journey evidence, and tests are committed |
| Runtime / Houdini CLI | verified-current | HFS contracts and all six disposable hython smokes pass under a clean process-local package boundary |
| Interactive GUI | pending | The installed Houdini package targets the main worktree, not this B candidate; every GUI checklist row remains NOT RUN |
| Real Vision provider | pending | No worktree `.env`, explicit Vision provider/model, or supported provider credential was present at pause time; no real call was attempted |
| Documentation / rules | changed-and-verified | This handoff is the current entry; `CLAUDE.md` points here and no longer advertises the superseded Runtime branch as active |
| Agent memory | not-applicable | No generated or platform memory was edited |
| Workspace | verified-current | Only the root and Runtime worktrees exist; this branch was clean and synchronized with origin before the handoff edit |

## Completed acceptance work

Stage A is fully accepted:

- source acceptance commit: `3298e13`;
- evidence commit: `4db806d`;
- full repository result: `3197 passed, 12 skipped`, zero warnings;
- frozen lock, full Ruff, scoped Mypy, compileall, and diff checks passed;
- acceptance record:
  `docs/superpowers/reviews/2026-07-22-stage-a-acceptance.md`.

Stage B commits completed before this pause:

1. `f6dfde8` - seed the Stage B acceptance ledger;
2. `bef4fe4` - add explicit provider-neutral Vision configuration and adapter;
3. `f88ab39` - distinguish attempted evaluation failures from unavailable
   preconditions;
4. `25e8913` - record offline and Houdini command-line gates;
5. `bf88745` - require strict real-provider delivery evidence using production
   Vision wiring;
6. `c4a2dd9` - record the current offline acceptance decision and GUI checklist.

The Stage B implementation now provides:

- explicit `EEE_VISION_PROVIDER` / `EEE_VISION_MODEL` opt-in without silently
  inheriting the text-model provider;
- provider construction through the existing registry, with no direct provider
  SDK imports in Houdini-side or Vision adapter code;
- verified ArtifactStore bytes as the only image source for evaluation;
- `UNAVAILABLE` for pre-invocation capability/configuration/artifact gaps and
  `FAILED` for timeout, exception, or invalid output after an evaluation starts;
- durable bounded `vision_status`, `vision_accepted`, `vision_reason_code`, and
  `vision_artifact_digest_match` evidence;
- failure and unavailable states projected distinctly into the panel.

## Fresh accepted evidence

The final offline candidate gate ran on
`bf887451024277696606ed64f814a17ecf7b9bd8`:

```text
uv lock --check
PASS

uv run --frozen ruff check .
PASS

uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision
Success: no issues found in 29 source files

python -m compileall -q eee_agent houdini_side tests
exit 0

uv run --frozen --extra eval pytest -q
3225 passed, 12 skipped, 0 warnings in 102.86s

git diff --check main...HEAD
exit 0
```

Houdini 21.0.440 / Python 3.11.7 evidence:

- opt-in HFS knowledge contracts: `11 passed, 3226 deselected` in 10.46s;
- Secure Bridge smoke: 20 checks passed;
- Artifact capture smoke: 21 checks passed;
- sensitivity smoke: 25 checks passed;
- ChangeSet, bootstrap, and golden-case smokes passed;
- the owned temporary Bridge state directory was empty after cleanup;
- no HIP, screenshot, provider payload, prompt, credential, process log, or
  third-party package output was committed.

The accepted clean process-local HFS boundary was:

```powershell
$hfs = 'C:/Program Files/Side Effects Software/Houdini 21.0.440'
$env:HOUDINI_PACKAGE_SKIP = '1'
$env:HOUDINI_NO_ENV_FILE = '1'
$env:HOUDINI_PATH = "$hfs/packages/apex;$hfs/packages/kinefx;&"
```

Use these variables only in the process performing the HFS checks. The default
user package set emits unrelated startup text on stdout; keep the repository's
strict inventory parser and do not change or copy output from those packages.

## Why Stage B is still NOT READY

Two substantive acceptance gates remain, followed by the final decision rerun:

1. **B-07 / RN-012 - interactive Houdini GUI checklist: NOT RUN.** The
   installed Houdini package currently points to the root `main` worktree. Do
   not start GUI acceptance until the user confirms that no unsaved scene is at
   risk and the exact committed B worktree is deliberately deployed. Re-read
   the installed package after the change. Codex must not infer GUI PASS;
   `docs/superpowers/reviews/2026-07-22-stage-b-gui-checklist.md` requires the
   user's observations.
2. **B-08 / RN-005 - real Vision provider journey: NOT RUN.** At pause time the
   worktree had no `.env`, no explicit `EEE_VISION_PROVIDER` or
   `EEE_VISION_MODEL`, and no supported provider credential in the process.
   Configure them locally without committing or printing values. PASS requires
   a completed normalized Vision result, exact Artifact ID/SHA match, bounded
   reason code, replay survival, and scene cleanup.
3. **B-09 - final readiness decision: NOT RUN.** After B-07 and B-08, rerun all
   final gates on one committed candidate, update the acceptance ledger and
   findings register, then record PASS or NOT READY without ambiguity.

The current authoritative records are:

- `docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md`;
- `docs/superpowers/reviews/2026-07-22-stage-b-gui-checklist.md`;
- `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`;
- `docs/superpowers/plans/2026-07-22-runtime-release-acceptance.md`.

RN-006 is resolved. RN-005 and high-severity RN-012 remain planned Stage B
gates. RN-013 remains an external environment finding with an accepted clean
process-local workaround. RN-011 belongs to Stage C and is out of scope until
Stage B passes.

## Resume order for 2026-07-24

1. Pull and verify the exact branch using the commands below.
2. Set `EEE_VISION_PROVIDER`, `EEE_VISION_MODEL`, and the selected provider's
   API credential in the process only. Optional bounded settings are
   `EEE_VISION_MAX_IMAGE_BYTES` and `EEE_VISION_TIMEOUT_SECONDS`. Never add
   secrets to `.env`, Git, chat, logs, or acceptance Markdown.
3. Confirm with the user that Houdini has no unsaved scene and that deploying
   the B worktree is authorized. Update the installed package target only after
   that confirmation, restart only owned processes, and re-read the package.
4. Run the opt-in provider/Artifact journey exactly once. Follow Task 6 in the
   Stage B plan; retain only bounded redacted evidence outside Git.
5. Have the user perform every GUI checklist item against the exact deployed B
   commit and record only the redacted results.
6. Run the complete B-09 static/offline gates, review all findings, and commit
   the final Stage B decision.
7. Only after an explicit Stage B PASS may a separate Stage C branch be created
   from the accepted B tip and the Task 19-C plan begin.

For the real-provider harness, the non-secret process controls are:

```powershell
$env:EEE_RUN_RUNTIME_MVP_PROVIDER_E2E = 'true'
$env:EEE_RUNTIME_MVP_PROVIDER_COMMAND = 'python tests/runtime/provider_journey.py'
$env:HFS = $hfs
uv run --frozen --extra eval pytest -q tests/runtime/runtime_mvp_provider_e2e.py -s
```

Do not launch a second provider journey while one is running. The harness owns
disposable Runtime state and validates bounded evidence without displaying the
provider subprocess output.

## Resume commands

```powershell
Set-Location Z:/EEE_Project/EEEProceduralModeling/.worktrees/runtime
git fetch origin
git switch feature/b-release-acceptance
git pull --ff-only origin feature/b-release-acceptance
git status --short --branch
Get-Content docs/handoffs/2026-07-23-runtime-stage-b-pause-handoff.md
```

Expected after pull:

- branch `feature/b-release-acceptance`;
- clean worktree synchronized with
  `origin/feature/b-release-acceptance`;
- HEAD at the commit containing this handoff;
- Stage B decision still **NOT READY** until B-07, B-08, and B-09 complete;
- root and Runtime worktrees preserved.

## Guardrails while paused

Do not merge or push to `main`, create an RC tag, start Stage C, delete either
worktree or any branch, rewrite accepted history, modify user Houdini packages
without the deployment precondition, or commit local evidence and credentials.
The review site remains intact for tomorrow; no cleanup is authorized by this
handoff.
