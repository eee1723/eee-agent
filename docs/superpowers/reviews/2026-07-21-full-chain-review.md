# Full-chain project review — 2026-07-21

Date: 2026-07-21
Scope: whole project on `feature/runtime` (commit `db07ca7`)
Reviewer: ZCode full-chain pass (3 parallel read-only sub-reviews + direct
verification)

This review was triggered by a user-reported regression ("I sent 你好 and got
no reply"). The root cause turned out to be one of a family of "data collected,
view-model supports it, but `main_window` never wired it" gaps left by the
three-pane panel migration. The pass audited the whole panel↔client↔backend
chain, design-doc consistency, stale code/docs, and forward planning.

## Summary

| Goal | Outcome |
|---|---|
| 1. End-to-end chain / leftover bugs | **6 real panel bugs found** (1 was the reported one). All fixed; the 7th (selection rendering) deferred by decision. |
| 2. Design-doc vs implementation | **No "claimed-done-but-missing" found.** One structural spec drift (VALIDATION tab) documented. |
| 3. Stale / contradictory code & docs | **5 HIGH/MEDIUM doc issues fixed** (Phoenix false claim, dead module refs, stale paths, dead link, spec drift). |
| 4. Gaps & forward plan | See §4 — MVP is offline-complete; S9 (GUI gate + Vision real-provider + RC tag), Task 19-C delivery, and richer asset batches are the open work. |
| 5. Extra checks | CI ruff scope widened to cover `houdini_side` + `tests/panel`; Phoenix dependency probe added. |
| 6. Docs & README | Updated README/CLAUDE/SETUP/handoff; README gained badges + highlights + accurate Status table. |

## 1. End-to-end chain — bugs found and fixed

All six are the **same regression class** as the reported "no reply": the
three-pane migration (Tasks 6–10) collected data in `client.py` /
`runtime_state.py` but never connected it to a widget in `main_window.py`.
The legacy single-file panel (`f2242bd`) rendered all of these.

| # | Bug | Severity | Fix commit |
|---|---|---|---|
| 1 | Run `output` (final_response) never rendered — **the reported "no reply"** | blocker | `1d57500` |
| 2 | Run `activity` (latest tool step) never rendered | major | `8732996` |
| 3 | `render_visions` wrote the same widget as `render_artifacts` and clobbered the artifact list — inspector showed neither properly | major | `db07ca7` |
| 4 | `changeset.approve`/`changeset.reject` succeeded silently — drawer just vanished, `approval_result_card` existed but was only used for the expired branch | major | `db07ca7` |
| 5 | Connection drop left the composer stuck in `running` (client silently drops commands while offline; no snapshot ever resets it) | major | `db07ca7` |
| 6 | Switching Session never cleared the conversation flow — multiple sessions' histories mixed in one stream (asymmetric with the artifacts/visions reset in the same method) | major | `db07ca7` |

### What was verified as correct (no action)

- **All 9 client signals** (`connectionChanged`/`sessionChanged`/`sessionsChanged`/`runtimeSnapshotChanged`/`changesetsChanged`/`commandSucceeded`/`commandFailed`/`artifactObserved`/`visionObserved`) have both an `emit` site in `client.py` **and** a `connect`+handler in `main_window.py`. No dangling signals. `SelectionQueryWorker`'s three signals likewise connected.
- **Output render ordering**: `message.assistant_final` is emitted before `run.state_changed→Completed`, so `_output[run_id]` is final by the time the terminal snapshot arrives — the `_maybe_render_output` guard has no race.
- **State fields**: every field initialized in `__init__` is read somewhere, and no handler reads a field before `__init__` sets it.
- **338 internal imports** across `eee_agent/`+`houdini_side/`+`tests/` all resolve — no runtime `ImportError` lurking.

### Deferred by decision

- **Selection rendering (was MAJOR-2 candidate):** `SelectionQueryWorker` still fetches the full `SceneQueryResult` (binding + selected_nodes), but `_selection_succeeded` only uses it for the `Bridge: ready` state and discards the node detail. **Decision: keep the worker** — it is the source of the Bridge status in the context bar (a real feature). Rendering the selected-node table is a genuine **new UI feature**, not a regression of the three-pane design (the new inspector is intentionally Run/Workspace/Artifacts; SCENE was a legacy-only tab). Tracked as a forward enhancement in §4, to be done with real-Houdini selection data rather than blind in a static review.

### Minor items noted, not fixed (pre-existing, low impact)

- A failed Run with no streamed output produces no error card (`failure_json` is never rendered). Legacy behaved the same. Suggested: emit an error-tone notice card with `message_for_user` on `Failed`.
- `_on_command_succeeded` does not read `active_workspace_id` from the `workspace.inspect` result shape, so the context-bar workspace id only updates after create/bind. Legacy behaved the same.
- Multiple actionable changesets: only the first opens the drawer; no "N more in queue" hint. Legacy showed a count banner.

## 2. Design-doc vs implementation consistency

**No "claimed-done-but-missing" and no "claimed-todo-but-already-done" were found.** This is the single strongest signal of project health: every Status-table row, handoff "completed" claim, and plan checkbox reflects reality.

Verified end-to-end:
- 5 read-only tools (`scene_status`/`query_scene`/`inspect_workspace`/`geometry_stats`/`work_status`) — `agent_tools.py`.
- Exactly 4 workspace commands, `changeset.approve` as the only public changeset decision (no public `apply`).
- 8 Golden Cases — `golden_cases.py`.
- Vision router + "cannot override deterministic failure" invariant.
- Three-pane panel: all 11 plan tasks have matching commits; `legacy.py` deleted; package-level boundary test in place.

### Spec drift fixed

- `2026-07-21-runtime-panel-three-pane-design.md` §3.1 listed 4 Inspector tabs (Run/Workspace/**Validation**/Artifacts); the implementation ships 3 (no VALIDATION — the Runtime protocol has no standalone validation channel; evidence flows through ChangeSet lifecycle + `vision.evaluation_completed`). Added an implementation note to the spec and the absence is locked by `test_runtime_panel_sources.py`.

## 3. Stale / contradictory code & docs — fixed

| ID | Issue | Fix |
|---|---|---|
| H1 | `start_phoenix.py` claimed "Phoenix installed via pyproject" and the menu item would crash with `ModuleNotFoundError`. Phoenix is **not** in the lockfile. | Corrected docstring; added a dependency probe before spawn with a clear install instruction. README Observability row changed from ✅ to an accurate ⚠️. |
| H2 | `houdini_side/runtime_panel.py` referenced as a current file in CLAUDE.md (×2) and the current handoff — it is now the `runtime_panel/` package. | Updated CLAUDE.md + handoff to the package path. |
| H3 | SETUP.md top pointed at the stale `2026-07-17-cross-machine` handoff while its own bottom + CLAUDE.md + memory/AGENTS.md name `2026-07-20-runtime-development-transfer` as current. | SETUP now points at the 2026-07-20 entry. |
| H4 | `open_panel` comment cited the deleted `houdini_side.launch`; it is actually called from `MainMenuCommon.xml`. | Comment corrected. |
| M2 | `install_menu.py` docstring hard-coded `Z:\EEE_Project\...` (another machine). | Replaced with `<repo>` placeholder. |
| M3 | README layout cited a non-existent `docs/AGENT_FIX_PLAN.md`. | Removed. |
| M4 | CI ruff scope (`eee_agent/runtime eee_agent/vision`) was narrower than the handoff's local command (which adds `houdini_side tests/panel`), so CI could miss panel lint regressions. | CI ruff scope widened to match. |
| M7 | CLAUDE.md layout row omitted `workspace_inspector.py`/`start_phoenix.py`. | Added. |

All dated handoff/plan/spec mentions of removed legacy modules (`eee_agent.bridge`, `eee_agent.tools`, `start_rpc.py`, `chat_panel.py`, `launch.py`, `rpyc`) are **intentional "do not restore" warnings** and were left in place.

## 4. Project gaps and forward plan

The architecture spec (`2026-07-13-houdini-general-agent-architecture-design.md`)
lays out six milestones. **Foundation, Runtime, Secure Bridge, Docked UI, and
Strict Modeling are implemented and offline-verified.** Capture/Vision/Eval is
partially done. The project is at the "offline MVP complete, real-world gates
pending" stage.

### Open gates (must close before an RC tag — all honestly documented as pending)

1. **Interactive Houdini 21 GUI checklist** (plan `2026-07-21-runtime-panel-three-pane.md` Task 11 Step 6). Three-pane layout thresholds, Chinese IME, approval mouse flow, reconnect/restart, auto-start backend reap, full MODEL→REVIEW→Approve→RESULT journey. *This is the gate the user was exercising when the "no reply" bug surfaced — now fixed.*
2. **Vision real-provider journey.** `_vision_provider()` in `runtime/__main__.py` returns `None` in production; the advisory vision seam is wired but has never run against a real vision model.
3. **Runtime RC tag** — gated on the two above.

### Forward development targets (from the roadmap and this review)

| Priority | Work | Notes |
|---|---|---|
| High | **Render selected-node detail** in the inspector (WORKSPACE tab) from `SelectionQueryWorker` results | Worker already fetches it; only rendering is missing. Needs real-Houdini selection data to validate formatting. |
| High | **Task 19-C delivery slice** — `DecisionSummary`, parameter guide, Phoenix/LangSmith export | The last unfinished roadmap item; advisory only, local correctness does not depend on it. |
| Medium | **Richer 18-G asset batches** — more Golden Cases beyond the 8 verified ones | Iterative; current 8 cover assembly/surface/boolean/copy/sweep/extrude. |
| Medium | **`VisionStatus.FAILED`** is defined but never assigned in production (provider failures use `UNAVAILABLE` + `vision.provider_failed`). Decide: wire it for a distinct UI state, or remove it. |
| Medium | **Failure feedback**: render `failure_json.message_for_user` as an error card on failed Runs (both legacy and new panel currently silent). |
| Low | **B2 — per-component subagents** (deferred, largest change; roadmap says "after model swap"). |
| Low | **Eval framework** (`eval/`) is scaffold-only; cases minimal. |
| Low | **Phoenix as a real optional extra** — either add an `[observability]` optional-dependency group to `pyproject.toml` or document the manual install (now at least fails cleanly). |

### Architectural strengths worth preserving

- **Qt-free cores + thin Qt shells verified by source-boundary tests** — the pattern that let the six bugs above be found and fixed confidently without a GUI. This is the right discipline and should be applied to any new panel surface.
- **No TODO/FIXME/HACK/NotImplemented scaffolding in production code** — the codebase is unusually clean of temporary implementations.
- **Deterministic failure precedence** (vision can't override a hard validator) is invariant-tested.
- **Offline-first testing** (~2940 tests, no live LLM/Houdini) with explicit opt-in real-provider/real-Houdini acceptance.

## Verification of this review's fixes

```text
pytest -q:                       2942 passed, 11 skipped
ruff (runtime+vision+houdini_side+tests/panel): All checks passed
compileall:                      passed
```

Commits on `feature/runtime`:
- `1d57500` fix: render run output as an assistant card
- `8732996` fix: show the latest tool activity in the inspector RUN tab
- `db07ca7` fix: close four panel data-flow gaps found in full-chain review
- (this commit) docs: full-chain review fixes (Phoenix probe, stale paths, spec drift, README/CLAUDE/SETUP accuracy)
