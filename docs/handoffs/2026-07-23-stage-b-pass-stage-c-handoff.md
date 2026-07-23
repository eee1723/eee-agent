# Runtime Stage B PASS → Stage C Handoff

Date: 2026-07-23
Owner: continued from Codex Stage B
Branch: `feature/b-release-acceptance` at `9fc058b` (Stage B **PASS**)
Next: `feature/c-task19c-delivery` (Stage C, branched from `9fc058b`)

## Stage B acceptance (recorded)

Stage B is accepted at `9fc058b`. See
`docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md`:

- B-01..B-06: PASS (offline, HFS, hython smokes).
- B-07 (interactive GUI): PASS — user-observed all 16 checklist rows against
  candidate `9bdf37b` loaded by the installed package.
- B-08 (real Vision provider): PASS — `qwen-vl-plus` via the DashScope
  OpenAI-compatible endpoint produced a completed normalized advisory result.
- B-09 (final readiness): PASS — static/offline gates green at `9bdf37b`
  (3254 passed, 11 skipped, 0 warnings); every required B gate PASS; tree
  clean; no unresolved blocker/high finding.

Findings closed this stage: RN-005 (real Vision provider) and RN-012 (package
deployment drift). Open findings carried forward: RN-007 (WSL env probe),
RN-013 (third-party Houdini package stdout pollution — accepted workaround),
RN-011 (Phoenix optional deps — deferred to Stage C).

## What landed on the B branch (past the `bf88745` pause tip)

1. `8fa033a` feat(panel): archive and delete sessions from the sidebar
2. `aa9bf5f` fix(panel): start a fresh session on Houdini restart
3. `2c21155` feat(prompt): translate the system prompt to Chinese, reply in Chinese
4. `25d8e69` fix(panel): import QAction from QtGui, not QtWidgets
5. `3601a27` fix(panel): tolerate slow backend cold-start in the launcher
6. `ee54aa8` fix(agent): route knowledge tools through the trusted cache, not bridge validator
7. `9bdf37b` fix(changesets): guard recover_critical_to_recovered (Mypy)
8. `f6175f3` docs(stage-b): refresh acceptance ledger + correct RN-012
9. `9fc058b` docs(stage-b): record Stage B PASS

Notable product changes a Stage C author must be aware of:
- Session archive/delete (`session.archive`, `session.delete`) are now wired
  end-to-end through the panel; `_PANEL_COMMANDS` gained these plus
  `changeset.recover`.
- The system prompt is Chinese with a "始终用中文回复用户" directive; capability
  tests assert Chinese phrasing.
- Knowledge tools (`search_houdini_knowledge`, `get_houdini_knowledge`) no
  longer route through `_call`→`_finish` (the bridge budget validator). They
  read directly from `RuntimeToolContext.knowledge` (`KnowledgeProvider`
  protocol). The old path misreported legitimate large results as
  `bridge.unavailable`.
- `RuntimeToolContext` now requires a `knowledge: KnowledgeProvider` field.
- A new `Recovered` ChangeSet terminal state + `changeset.recover` command
  exist (manual CriticalRecovery exit).

## Stage C scope (Task 19-C: delivery + observability)

Plan: `docs/superpowers/plans/2026-07-22-task19c-delivery-observability.md`
Spec: `docs/superpowers/specs/2026-07-22-task19c-delivery-observability-design.md`

Stage C branches ONLY from this accepted B tip (`9fc058b`). Task 1 of the plan
hard-gates on `rg "Decision: PASS|Stage B: PASS"` in the Stage B acceptance
file — that now reads PASS.

User-visible deliverables (Stage C spec lines 82-96, plan Task 6):
- A compact **delivery card** in the conversation after each modeling journey
  (status / validation / receipt / Artifact+Vision availability / next action).
- A structured **DELIVERY Inspector tab** with a parameter table (name /
  current value / range-or-choices / sensitivity) telling the user how to
  adjust the result.
- A `DecisionSummary` aggregate built from already-durable trusted records
  (NOT a second source of truth).
- Optional, fail-isolated observability: a local span/telemetry sink that does
  NOT require Phoenix; an optional Phoenix adapter gated behind
  `EEE_OBSERVABILITY_PROVIDER=phoenix`.

### ⚠️ Stage C migration conflict (must fix before Task 4)

The Stage C plan Task 4 specifies SQLite migration **v7** for the
`delivery_summaries` table. **`SCHEMA_VERSION` is already 7** — it was used by
the Stage B CriticalRecovery `Recovered` terminal state
(`eee_agent/runtime/migrations.py`). Stage C Task 4 must use **v8** instead.
The handoff also notes `eee_agent.vision.evaluation.DeliveryEvaluation`
carries bounded brief/spec text; Stage C treats that as compatibility input
only and the new summary stores digests/references, not copies.

## Roadmap guardrails (unchanged)

- Only one stage branch active at a time in the single `.worktrees/runtime`
  worktree (the installed Houdini package resolves `EEE_PATH` to it).
- Do not merge main, rewrite accepted history, or weaken the trusted
  Workspace / typed ChangeSet / exact approval / preflight / transactional
  Apply / receipt / rollback / restart-recovery / model-and-public-write-tool
  exclusion / single-FIFO boundaries. Preserve the Runtime reconnect and
  Chinese IME fixes.
- No remote telemetry by default; local correctness must not depend on
  Phoenix/LangSmith.
