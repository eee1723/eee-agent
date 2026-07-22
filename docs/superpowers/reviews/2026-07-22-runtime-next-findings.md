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
| RN-003 | High | Planned A | `run.failed` persists `message_for_user`, panel state retains `failure_json`, but live and historical UI render an empty Assistant card when no text was produced. Users cannot distinguish failure from no response. | Confirmed Runtime-to-widget data-flow gap. | Add one strict failure view used by live rendering, replay, and structured Run inspector; verify every terminal state is visible. |
| RN-004 | High | Planned A | The 2026-07-21 full-chain review found six separate cases where data existed in client/state but was never wired into `main_window`. Current history behavior tests execute extracted AST methods with fakes rather than the complete Qt signal/widget chain. | Architectural coverage gap confirmed; exact minimal additional gate requires design in A3. | Add terminal-result and signal-to-handler contracts without adding PySide6 to the Foundation lock; real GUI remains a B gate. |
| RN-005 | Medium | Planned B | Production `_vision_provider()` returns `None`; offline Vision routing and UI exist but no production real-provider path has been accepted. | Confirmed missing release capability, not a failing deterministic path. | Implement explicit provider-registry selection and run a real Vision provider journey; unavailable remains valid when unconfigured. |
| RN-006 | Medium | Planned B | `VisionStatus.FAILED` exists but production provider failures currently use `UNAVAILABLE` plus `vision.provider_failed`. The product distinction is unproven. | Open semantic design question that requires real-provider evidence. | Decide from the B real-provider journey; wire a distinct state only if it changes user action, otherwise remove the unused enum value. |
| RN-007 | Medium | Investigating | Running `bash scripts/env_probe.sh` through this Windows tool's WSL Bash found the venv but could not execute `.venv/Scripts/python.exe` (`Exec format error`), producing a misleading `build_agent()` warning. Direct PowerShell/uv version checks succeeded. | Single hypothesis: the script assumes Git Bash/Windows process semantics and the failure is specific to WSL invocation. | Reproduce through Claude Code's actual Windows Bash hook and classify as product portability bug or tool-environment mismatch. Do not change the script before that evidence. |
| RN-008 | High | External gate | Three sequential no-tool probes with exact `--model "glm-5.2[1m]"` were run after the user refreshed login. Attempt 1 waited 190.8s and returned 401 before inference; attempts 2 and 3 waited 190.6s/186.8s and returned gateway 529 `[1305] 该模型当前访问量过大`; all returned `input_tokens=0`, `output_tokens=0`, and no tool turns. | Authentication is now confirmed, but the GLM gateway remains overloaded; no implementation request has reached a model turn. | Keep GLM 5.2 mandatory, do not substitute `k3` or start duplicate workers. Retry only after gateway capacity recovers; the A implementation branch remains clean and unmodified. |
| RN-009 | Medium | Resolved | Root worktree was on deleted-upstream `wip/pre-migration-main`; local `main` was 248 commits behind; duplicate review/runtime worktrees increased wrong-directory risk. | Confirmed repository hygiene issue. | `bc351c0` is retained by local tag `archive/pre-migration-main-2026-07-22`; `.zcode` and the differing old lock were moved to the external 2026-07-22 archive whose manifest SHA-256 is `be5c8fa65a6b8b2bb20908d69c1910be73eb12f12f89ba69643b0281c7c1496e`; root is clean local `main` at `9125ffd`; only root and the fixed Runtime worktree remain. The two initially inventoried Kimi ZIPs were already absent before the archive move, which the manifest records explicitly. |
| RN-010 | High | Resolved | Installed Houdini package points `EEE_PATH` at `.worktrees/runtime`; deleting or renaming that worktree would break the next load. A Houdini process seen during discovery had exited before cleanup execution. | Confirmed external path dependency. | Preserved `E:/eee-agent/.worktrees/runtime` in place, retained ignored `.env`/`.venv`, switched only its branch to `feature/a-stability` at `9125ffd`, and re-read the installed package JSON to verify the same `EEE_PATH`. |
| RN-011 | Medium | Planned C | Phoenix launcher exists but Phoenix dependencies are absent from the lockfile; older documentation previously overstated support. | Confirmed packaging/support gap; launcher now fails more clearly but supported installation remains unresolved. | Either define and verify an optional observability dependency group in C or keep the feature explicitly unsupported/manual. |

## Update rules

For every new finding:

1. Add evidence before proposing a fix.
2. Record one root-cause hypothesis at a time when the cause is not confirmed.
3. Assign severity from product impact, not implementation size.
4. Link the finding from the active plan only after disposition is reviewed.
5. Mark Resolved only with a commit and fresh verification command/output.
6. Preserve rejected hypotheses and external blockers when they materially
   explain why work changed direction.
