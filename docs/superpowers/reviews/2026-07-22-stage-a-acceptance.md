# Stage A Runtime Stability Acceptance

Date: 2026-07-22
Decision: **PASS**
Accepted source commit: `3298e138f68039d8eb74bb154f823b0e3db79eea`
Branch: `feature/a-stability`

## Environment

- Python: `3.11.15`
- uv: `0.11.28` (`ebf0f43d7`, Windows x86-64)
- The acceptance shell prepended the project's relative `.venv/Scripts`
  directory to `PATH`. Without that activation, this machine's bare `python`
  command resolves to the Windows Store placeholder and cannot run
  `compileall`; no repository file or product behavior was changed.

## Complete-diff audit

`git diff main...HEAD --stat` reported 84 files changed, 7,219 insertions,
and 1,295 deletions. The planned Stage A UI delta is confined to
`view_models.py`, `main_window.py`, `inspector.py`, and their panel tests; the
quality-gate repair changes pytest warning policy and closes the database owned
by the failing test. The remaining production changes are the reviewed strict
typing/contract batches recorded on this branch.

Audit result:

- Raw technical evidence does not reach a widget. `FailureView` retains only
  bounded `code`, user-facing `message`, `retryable`, and fixed presentation
  tone. Technical references, traceback data, provider payloads, and unknown
  fields are discarded before rendering.
- History replay and live terminal settling both call
  `terminal_result_items`; neither path reimplements terminal-state policy.
- Terminal rendering is idempotent through `_shown_output_run_id`, including
  repeated snapshots and the history-to-live handoff.
- No Runtime protocol or production database workaround was introduced for
  the UI fix. The RN-002 database correction is test teardown only.
- No unrelated UI redesign, retry command, automatic retry, or dependency on
  PySide6 entered Stage A.
- No new defect was found during the final audit. Existing future and external
  gates remain in the Runtime Next findings register.

## Fresh acceptance gates

All commands below ran from the accepted source commit after the environment
activation described above.

| Command | Exit | Result | Duration |
| --- | ---: | --- | ---: |
| `uv lock --check` | 0 | lock resolves 90 packages | 0.04 s |
| `uv run --frozen ruff check .` | 0 | all checks passed | 0.07 s |
| `uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision` | 0 | no issues in 28 source files | 1.81 s |
| `python -m compileall -q eee_agent houdini_side tests` | 0 | no diagnostics | 0.15 s |
| `uv run --frozen --extra eval pytest -q` | 0 | 3,197 passed; 12 skipped; 0 warnings | 107.93 s (pytest: 106.30 s) |
| `git diff --check main...HEAD` | 0 | no whitespace errors | 0.06 s |
| `git status --short --branch` | 0 | clean; tracking `origin/feature/a-stability` | 0.04 s |

Focused terminal-rendering and inspector contracts were also rerun:

```text
uv run --frozen pytest -q tests/panel/test_runtime_panel_view_models.py tests/panel/test_main_window_history.py tests/panel/test_main_window_history_behavior.py tests/panel/test_runtime_panel_sources.py
99 passed in 0.21s; exit 0; wall duration 1.01s
```

## Enumerated skips

The following 11 Houdini knowledge contract tests require
`EEE_RUN_HOUDINI_KB_TESTS=true` plus a local Houdini 21.0.440 HFS and hython:

1. `test_houdini_build_is_21_0_440`
2. `test_corpus_entity_counts_in_audited_ranges`
3. `test_required_operators_verified_at_build`
4. `test_every_verified_operator_exists_in_inventory`
5. `test_resolved_edge_targets_exist`
6. `test_manifest_records_unresolved_and_ambiguous_counts`
7. `test_manifest_hash_is_sha256`
8. `test_no_absolute_machine_path_stored`
9. `test_sop_source_scope_is_exact`
10. `test_four_project_skills_represented`
11. `test_cache_passes_writer_self_checks`

`test_wsl_availability_probe_executes_windows_python` was skipped because WSL
or the Windows probe venv was unavailable. These are known environmental gates,
not unexpected test suppression.

## RN-001 through RN-004 closure

- **RN-001 — Resolved:** `61c509d` removes the unused test import. Fresh
  repository Ruff passes.
- **RN-002 — Resolved:** `61c509d` closes the test-owned aiosqlite database and
  makes unhandled pytest thread exceptions fatal. Full acceptance reports zero
  warnings.
- **RN-003 — Resolved:** `596cf72`, `df648e0`, and `8843869` add bounded failure
  projection, one terminal selector for live/history, and structured inspector
  rendering. Failed runs no longer become empty Assistant cards.
- **RN-004 — Resolved for Stage A:** `df648e0` adds deterministic behavior
  contracts for selector delegation, terminal ordering, and deduplication;
  `8843869` covers the inspector binding. The real Qt/Houdini journey remains
  explicitly assigned to Stage B rather than hidden by a Foundation-only test.

## Decision

Stage A satisfies its zero-error Mypy requirement and all repository quality
gates at the accepted source commit. The evidence delta contains documentation
only and is subject to a separate staged diff check before commit and a clean
worktree check afterward. Stage B may start only from that clean evidence
commit; Stage A is not merged into `main` here.
