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
| B-02 | NOT SET | Offline Python 3.11 | Provider-neutral Vision configuration and adapter tests | Explicit opt-in; bounded verified Artifact bytes; no direct SDK import | NOT RUN | — | RN-005 |
| B-03 | NOT SET | Offline Python 3.11 | Vision FAILED/UNAVAILABLE contract and delivery tests | Pre-invocation gaps unavailable; attempted evaluation failures failed | NOT RUN | — | RN-006 |
| B-04 | NOT SET | Offline Python 3.11 | Frozen lock, Ruff, Mypy, compileall, full pytest | All pass; zero unexpected warnings; skips enumerated | NOT RUN | — | — |
| B-05 | NOT SET | Houdini 21.0.440 / hython 3.11 | Opt-in HFS knowledge contracts | 11 pass | NOT RUN | — | — |
| B-06 | NOT SET | Disposable hython | Bridge, ChangeSet, capture, sensitivity, bootstrap, golden smokes | All pass; owned state cleaned; no saved HIP | NOT RUN | — | — |
| B-07 | NOT SET | Interactive Houdini panel | Complete GUI and lifecycle checklist | Every required item PASS with user-observed evidence | NOT RUN | Installed package does not yet target B worktree | RN-012 |
| B-08 | NOT SET | Explicit real Vision provider | Real provider + exact ArtifactStore byte journey | Completed normalized advisory result; digest match; cleanup | NOT RUN | — | RN-005, RN-006 |
| B-09 | NOT SET | Final committed candidate | Repeat full gates and Codex evidence/code review | Clean tree; no blocker/high finding; every required B gate PASS | NOT RUN | — | — |

## Candidate notes

- The repository is currently hosted in a different machine-local workspace
  than the historical path written in the approved plan. This ledger never
  commits either path. The installed Houdini package target must be discovered
  and verified before B-07; a mismatch is a failed precondition, not an
  implicit deployment authorization.
- Stage B does not tag, publish an RC, merge, delete a branch, or weaken an
  external gate.
