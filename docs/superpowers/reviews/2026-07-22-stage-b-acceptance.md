# Stage B Runtime Release Acceptance

Date opened: 2026-07-22
Candidate base: `4db806d595ee5412b5c3d18626927b3fd54fef68`
Branch: `feature/b-release-acceptance`
Machine-local evidence: `[machine-local acceptance root]`
Current decision: **NOT READY**

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
| B-07 | NOT SET | Interactive Houdini panel | Complete GUI and lifecycle checklist | Every required item PASS with user-observed evidence | NOT RUN | Installed package does not yet target B worktree | RN-012 |
| B-08 | `bf88745` | Explicit real Vision provider | Real provider + exact ArtifactStore byte journey | Completed normalized advisory result; digest match; cleanup | NOT RUN | Opt-in harness returned `provider_credentials_unavailable`; no real call was attempted | RN-005 |
| B-09 | `bf88745` | Final committed candidate | Repeat full static/offline gates and Codex evidence/code review | Clean tree; no blocker/high finding; every required B gate PASS | NOT RUN | Static/offline gates pass; final readiness withheld while B-07 and B-08 remain NOT RUN | RN-005, RN-012 |

## Candidate notes

- The repository is currently hosted in a different machine-local workspace
  than the historical path written in the approved plan. This ledger never
  commits either path. The installed Houdini package target must be discovered
  and verified before B-07; a mismatch is a failed precondition, not an
  implicit deployment authorization.
- Stage B does not tag, publish an RC, merge, delete a branch, or weaken an
  external gate.

## Final offline candidate gate

At `bf887451024277696606ed64f814a17ecf7b9bd8`:

- `uv lock --check`: exit 0;
- frozen Ruff: exit 0;
- targeted Mypy for `eee_agent/panel eee_agent/runtime eee_agent/vision`: exit 0,
  29 source files;
- `python -m compileall -q eee_agent houdini_side tests`: exit 0;
- frozen full pytest: `3225 passed, 12 skipped, 0 warnings` in 102.86s;
- `git diff --check main...HEAD`: exit 0.

The 12 skips are the same 11 opt-in HFS tests and one WSL probe listed in the
Stage A evidence; B-05 was separately run with the clean HFS package boundary
and passed all 11. This offline result is not a release PASS because B-07
requires user-observed GUI confirmation and B-08 requires an explicitly
configured real Vision provider.
