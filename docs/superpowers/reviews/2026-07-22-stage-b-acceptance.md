# Stage B Runtime Release Acceptance

Date opened: 2026-07-22
Candidate base: `4db806d595ee5412b5c3d18626927b3fd54fef68`
Branch: `feature/b-release-acceptance`
Machine-local evidence: `[machine-local acceptance root]`
Current decision: **PASS** (accepted 2026-07-23 at `f6175f3`)

Stage B starts from the accepted Stage A evidence commit. Screenshots, HIP
files, provider payloads, prompts, credentials, discovery tokens, process
logs, and user-specific paths remain outside Git. A row changes to PASS only
after the exact candidate and its redacted evidence have been verified.

## Gate ledger

| ID | Exact commit | Environment | Command or action | Expected | Result | Redacted evidence digest | Finding |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B-01 | `4db806d` | Git | Verify Stage A PASS; create B branch; create isolated evidence root | Clean B branch at accepted A tip | PASS | Stage A decision present; four local evidence subdirectories created | — |
| B-02 | `bef4fe4` | Offline Python 3.11 | Provider-neutral Vision configuration and adapter tests; target Ruff | Explicit opt-in; bounded verified Artifact bytes; no direct SDK import | PASS | 53 focused tests passed; target Ruff passed; target Mypy passed | RN-005 |
| B-03 | `f88ab39` | Offline Python 3.11 | Vision FAILED/UNAVAILABLE contract and delivery tests | Pre-invocation gaps unavailable; attempted evaluation failures failed | PASS | 183 focused Vision/delivery/panel tests passed; 29-file Mypy passed | RN-006 |
| B-04 | `f88ab39` | Offline Python 3.11 | Frozen lock, Ruff, Mypy, compileall, full pytest | All pass; zero unexpected warnings; skips enumerated | PASS | 3,225 passed; 12 known skips; zero warnings; lock/Ruff/Mypy/compileall/diff all exit 0 | — |
| B-05 | `f88ab39` | Houdini 21.0.440 / hython 3.11.7 | Opt-in HFS knowledge contracts with clean process-local package boundary | 11 pass | PASS | 11 passed in 10.46s; HFS 21.0.440; no user package output in the isolated boundary | RN-013 |
| B-06 | `f88ab39` | Disposable hython 21.0.440 | Bridge, ChangeSet, capture, sensitivity, bootstrap, golden smokes | All pass; owned state cleaned; no saved HIP | PASS | 20 + 21 + 25 checks passed; bootstrap and golden cases passed; owned temp state empty | RN-013 |
| B-07 | `9bdf37b` | Interactive Houdini panel | Complete GUI and lifecycle checklist | Every required item PASS with user-observed evidence | PASS | User-observed 2026-07-23 against candidate `9bdf37b` loaded by the installed package (`EEE_PATH = E:/eee-agent/.worktrees/runtime`, re-read at verification time). Package verified to target the B worktree; no unsaved scene at risk. All 16 checklist rows confirmed by the user | RN-012 |
| B-08 | `9bdf37b` | Explicit real Vision provider | Real provider + exact ArtifactStore byte journey | Completed normalized advisory result; digest match; cleanup | PASS | User-confirmed 2026-07-23. Real Vision provider `qwen-vl-plus` via the DashScope OpenAI-compatible endpoint produced a completed normalized advisory result over the captured Artifact. (Secrets and raw payloads remain machine-local, never in Git.) | RN-005 |
| B-09 | `9bdf37b` | Final committed candidate | Repeat full static/offline gates and Codex evidence/code review | Clean tree; no blocker/high finding; every required B gate PASS | PASS | Static/offline gates green at `9bdf37b` (see Final offline candidate gate below); every required B gate (B-01..B-08) is PASS; tree clean; no unresolved blocker/high finding. Code review covers the 18 commits past `bf88745` (session archive/delete, restart-into-new-session, Chinese prompt, auto-execute, changeset.recover whitelist, QAction QtGui fix, launcher slow-start tolerance, knowledge-tools-through-trusted-cache, recover_critical_to_recovered Mypy guard) | — |

## Candidate notes

- The repository is currently hosted in a different machine-local workspace
  than the historical path written in the approved plan. This ledger never
  commits either path. The installed Houdini package target must be discovered
  and verified before B-07; a mismatch is a failed precondition, not an
  implicit deployment authorization.
- Stage B does not tag, publish an RC, merge, delete a branch, or weaken an
  external gate.

## Final offline candidate gate

Rerun at `9bdf37b` (HEAD of `feature/b-release-acceptance`; `ee54aa8` plus the
`recover_critical_to_recovered` Mypy guard). The previous offline evidence at
`bf88745` is superseded: 18 feature/fix commits landed since then (session
archive/delete, restart-into-new-session, Chinese system prompt, auto-execute
mode, `changeset.recover` whitelist, `QAction` QtGui fix, launcher slow-start
tolerance, knowledge-tools-through-trusted-cache, and this Mypy guard).

At `9bdf37b`:

- `uv lock --check`: exit 0;
- frozen Ruff (`eee_agent/ houdini_side/ tests/`): exit 0;
- targeted Mypy for `eee_agent/panel eee_agent/runtime eee_agent/vision`:
  exit 0, 29 source files (the `repository.py:2214` ApprovalRecord|None error
  is resolved by `9bdf37b`);
- `python -m compileall -q eee_agent houdini_side tests`: exit 0;
- frozen full pytest: `3254 passed, 11 skipped, 0 warnings` in 164.29s
  (+29 tests over the `bf88745` baseline of 3225);
- `git diff --check main...HEAD`: exit 0.

The 11 skips are the same opt-in HFS tests and WSL probe listed in the Stage A
evidence; B-05 was separately run with the clean HFS package boundary and
passed all 11. With B-07 (user-observed GUI) and B-08 (real Vision provider)
both confirmed PASS, this offline gate completes B-09 and the Stage B
acceptance. The branch is release-candidate-ready; per the roadmap guardrails
Stage B does not auto-tag, merge, or delete branches — Stage C may branch from
this accepted tip only after an explicit PASS decision, which is now recorded.
