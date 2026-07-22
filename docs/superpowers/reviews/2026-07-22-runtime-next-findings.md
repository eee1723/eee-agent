# Runtime Next Findings Register

Date opened: 2026-07-22
Scope: A stability -> B release acceptance -> C Task 19-C

This is the live evidence register for defects, integration gaps, external
gates, and architectural concerns discovered while executing the Runtime Next
roadmap. A finding is not automatically bundled into the active task. Its
disposition follows the triage rules in
`docs/superpowers/specs/2026-07-22-runtime-next-roadmap-design.md`.

## Status vocabulary

- **Open**: evidence is sufficient to retain the issue; no accepted resolution.
- **Investigating**: one explicit root-cause hypothesis is being tested.
- **Planned A/B/C**: assigned to a reviewed stage design.
- **External gate**: progress depends on a service, credential, license, or
  user-operated environment; product boundaries remain unchanged.
- **Resolved**: a reviewed commit and fresh verification evidence exist.
- **Closed-no-change**: investigation proved no product or repository defect;
  the evidence and rationale remain recorded.

## Findings

| ID | Severity | Status | Evidence and impact | Root-cause status | Disposition / verification |
| --- | --- | --- | --- | --- | --- |
| RN-001 | High | Resolved | Repository Ruff command failed on an unused `pytest` import in `tests/panel/test_main_window_history_behavior.py`. | Confirmed: unused import introduced by `05e0ca1`. | Fixed in `61c509d`. Codex independently ran Ruff over all three A-1 files (`All checks passed`) and the history behavior suite (`7 passed`). |
| RN-002 | High | Resolved | Full pytest reported `PytestUnhandledThreadExceptionWarning`; the two-test Todo sequence reproduced a leaked aiosqlite worker reporting into a closed loop. | Confirmed: `test_d2_update_todos_rejects_non_list` discarded the database and executed `finally: pass`. | Fixed in `61c509d`: the owning test closes its database and the exact unhandled-thread warning category is now fatal. Codex independently verified the reproducer (`2 passed`) and full Run repository suite (`118 passed`) with no warning. |
| RN-003 | High | Resolved | `run.failed` persists `message_for_user`, panel state retains `failure_json`, but live and historical UI rendered an empty Assistant card when no text was produced. Users could not distinguish failure from no response. | Confirmed Runtime-to-widget data-flow gap. | Fixed by `596cf72`, `df648e0`, and `8843869`: one bounded `FailureView` feeds the shared live/history terminal selector and structured Run inspector. Fresh focused panel contracts: `99 passed`; full repository acceptance: `3197 passed, 12 skipped`, zero warnings. |
| RN-004 | High | Resolved | The 2026-07-21 full-chain review found six separate cases where data existed in client/state but was never wired into `main_window`. History behavior tests previously did not cover the unified terminal path and its idempotency contract. | Architectural coverage gap confirmed; Stage A's deterministic non-Qt boundary was selected while the real GUI journey remains a B gate. | `df648e0` adds behavior contracts for replay/live selector delegation, partial output ordering, repeated terminal snapshots, and history-to-live deduplication; `8843869` adds the inspector source contract without adding PySide6 to the Foundation lock. Fresh focused panel contracts: `99 passed`. |
| RN-005 | Medium | Planned B | Production `_vision_provider()` returns `None`; offline Vision routing and UI exist but no production real-provider path has been accepted. | Confirmed missing release capability, not a failing deterministic path. | Implement explicit provider-registry selection and run a real Vision provider journey; unavailable remains valid when unconfigured. |
| RN-006 | Medium | Planned B | `VisionStatus.FAILED` exists but production provider failures currently use `UNAVAILABLE` plus `vision.provider_failed`. The product distinction is unproven. | Open semantic design question that requires real-provider evidence. | Decide from the B real-provider journey; wire a distinct state only if it changes user action, otherwise remove the unused enum value. |
| RN-007 | Medium | Investigating | Running `bash scripts/env_probe.sh` through this Windows tool's WSL Bash found the venv but could not execute `.venv/Scripts/python.exe` (`Exec format error`), producing a misleading `build_agent()` warning. Direct PowerShell/uv version checks succeeded. | Single hypothesis: the script assumes Git Bash/Windows process semantics and the failure is specific to WSL invocation. | Reproduce through Claude Code's actual Windows Bash hook and classify as product portability bug or tool-environment mismatch. Do not change the script before that evidence. |
| RN-008 | High | Closed-no-change | Three sequential GLM 5.2 probes failed before an implementation turn because of authentication/gateway availability. | External worker availability was confirmed; no repository defect was implicated. | The user withdrew the GLM worker requirement and directed Codex to implement the reviewed plan directly. No product change or fallback model was introduced. |
| RN-009 | Medium | Resolved | Root worktree was on deleted-upstream `wip/pre-migration-main`; local `main` was 248 commits behind; duplicate review/runtime worktrees increased wrong-directory risk. | Confirmed repository hygiene issue. | `bc351c0` is retained by local tag `archive/pre-migration-main-2026-07-22`; `.zcode` and the differing old lock were moved to the external 2026-07-22 archive whose manifest SHA-256 is `be5c8fa65a6b8b2bb20908d69c1910be73eb12f12f89ba69643b0281c7c1496e`; root is clean local `main` at `9125ffd`; only root and the fixed Runtime worktree remain. The two initially inventoried Kimi ZIPs were already absent before the archive move, which the manifest records explicitly. |
| RN-010 | High | Resolved | During the original workspace cleanup, the installed Houdini package pointed `EEE_PATH` at the Runtime worktree; deleting or renaming it would have broken the next load. | Confirmed external path dependency at the time of cleanup. | The Runtime worktree was preserved through Stage A. The machine was subsequently moved to a different workspace layout; current Stage B package state is tracked separately as RN-012. |
| RN-011 | Medium | Planned C | Phoenix launcher exists but Phoenix dependencies are absent from the lockfile; older documentation previously overstated support. | Confirmed packaging/support gap; launcher now fails more clearly but supported installation remains unresolved. | Either define and verify an optional observability dependency group in C or keep the feature explicitly unsupported/manual. |
| RN-012 | High | Planned B | At the B-01 precondition audit, the installed Houdini 21.0 package targeted the current main worktree rather than the clean `feature/b-release-acceptance` Runtime worktree. Loading Houdini now would exercise `main` at `5a6880b`, not the accepted B candidate. | Confirmed machine-local deployment drift after the workspace move; no code defect is indicated. | Keep offline B development on the Runtime worktree. Before B-07, verify there is no unsaved user scene, deploy the exact committed B candidate through a reviewed package target, restart only owned processes, and re-read the package before recording GUI evidence. |

## Update rules

For every new finding:

1. Add evidence before proposing a fix.
2. Record one root-cause hypothesis at a time when the cause is not confirmed.
3. Assign severity from product impact, not implementation size.
4. Link the finding from the active plan only after disposition is reviewed.
5. Mark Resolved only with a commit and fresh verification command/output.
6. Preserve rejected hypotheses and external blockers when they materially
   explain why work changed direction.
