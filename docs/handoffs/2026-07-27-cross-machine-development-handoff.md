# Cross-machine development handoff — 2026-07-27

This is the current continuation entry point for
`feature/html-to-houdini-pipeline`. Older handoffs remain milestone evidence;
when their status conflicts with this file, use this file plus the current code
and test results.

## Repository state

- GitHub: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/html-to-houdini-pipeline`
- Baseline immediately before this documentation closeout:
  `5cc954a27806a748168b7093ced7fcdd61c24c61`
- The branch contains the HTML-to-Houdini validation work, scratch-native
  Provider evidence migration, task graph/node lifecycle implementation, and
  adversarial cleanup-path hardening.
- The three previously local-only guides under `docs/html/` are now tracked so
  they survive the machine transfer. They are orientation material, not
  acceptance evidence.

On the new machine, confirm the actual remote tip instead of relying on a copied
SHA:

```powershell
git status --short --branch
git log -1 --oneline --decorate
```

## Resume on a new Windows machine

```powershell
git clone https://github.com/eee1723/eee-agent.git
Set-Location eee-agent
git fetch origin
git switch --track origin/feature/html-to-houdini-pipeline
uv sync --frozen --extra eval
uv run --frozen --extra eval python -m eee_agent.cli versions
uv run --frozen --extra eval pytest -q
```

Do not copy `.venv`, Runtime SQLite files, `runtime.token`, or Houdini-local
state between machines. Houdini is an external machine dependency. Probe its
installation and use that installation's `bin\hython.exe`; the previously
verified path was:

```text
C:\Program Files\Side Effects Software\Houdini 21.0.440\bin\hython.exe
```

If the new machine uses another path, set `EEE_HFS` locally or substitute the
actual executable path in the smoke commands.

## Verified baseline

- Full offline gate: `3549 passed, 11 skipped` (includes the Windows
  RuntimeLock deflake cherry-picked from main and two new output-resolution
  tests).
- Dependency/version probe:
  `uv run --frozen --extra eval python -m eee_agent.cli versions` passed.
- Chrome HTML sketch quality smoke passed with a nonblank 1440×900 PNG.
- Scratch build/verify/commit, Bridge journey, task storage, lifecycle cleanup,
  bounded agent summary, and the Qt-free panel projection are covered by the
  offline suite.
- Real Hython task-graph smoke reached build, commit, layout, topology, and
  allowlisted deletion.
- The cleanup-path adversarial review fixed traversal and nested-target
  validation in commit `8d90cf4`.

## Open acceptance work

1. ~~Houdini display/render flags~~ **RESOLVED (2026-07-27).** The fresh-read
   failure was not a Houdini lifecycle/deferred-state issue: `scratch_exec`
   never sets the sandbox display flag, so the flag stayed on the accidental
   first-created node (`box1`), and `_scratch_output_node` preferred that
   flag holder over the chain end — commit verified and flagged the wrong
   node. Fix: `_scratch_output_node` now prefers the unique terminal sink
   (child with no downstream connections), falling back to the flag holder
   then the last child. Verified on Houdini 21.0.440 (`D:\houdini`):
   `tests/runtime/task_graph_houdini_smoke.py` prints `SMOKE OK` with no
   external reassert; `SCRATCH SMOKE OK` and `SCRATCH BRIDGE JOURNEY OK`
   regression-passed; offline gate 3549 passed, 11 skipped.

2. ~~Real Provider acceptance~~ **RESOLVED (2026-07-27).** The strict harness
   passed 3/3 consecutive real DeepSeek runs on Houdini 21.0.440 (`D:\houdini`):
   `{"status": "passed", "reason": "provider_journey_completed"}` each time.
   Every run selected `scratch_build`, ran `verify_geometry`, committed via
   `scratch_commit`, passed final-geometry readback, proved sandbox absence,
   restart replay, and worker cleanup. Three defects were found and fixed to
   get here: (a) `verify_geometry`'s `eval` package import depended on the
   launch mode (repo root on `sys.path`) — `sketch_tools.py` now resolves it
   relative to the module; (b) `ScratchCoordinator.commit` forwarded its
   internal pair-tuple into the provider's `Mapping` contract, crashing every
   annotated commit — converted to `dict` at the seam, with a regression test
   (the fake provider had encoded the wrong contract, which is why offline
   tests stayed green); (c) the journey misreported the bridge's
   `bridge.houdini_read_failed` ("node not found") for the removed sandbox as
   "sandbox still exists" — absence is now proven by empty result OR the
   bounded not-found error. Note for reruns: `EEE_RUNTIME_MVP_PROVIDER_COMMAND`
   must name the project venv interpreter explicitly (a bare `python` resolves
   to the PyManager 3.14 on this machine), e.g.
   `.venv\Scripts\python.exe tests/runtime/provider_journey.py`, and the
   credential must be injected into the harness process environment (the
   harness intentionally does not load `.env` itself).

3. **Wave B:** B1 Chrome quality gate is complete. **B2 L4 two-round
   sessions COMPLETE (2026-07-27):** `tests/runtime/html_session_journey.py`
   passed all three simple cases with real DeepSeek on Houdini 21.0.440 —
   chair (bbox 0.46×0.90×0.45, grounded), desk (1.20×0.72×0.60), shelf
   (0.80×1.50×0.30). Every run: sketch rendered with zero pre-approval
   Houdini writes, same-session build→verify→commit, in-envelope final
   geometry, sandbox absent, restart replay, bounded worker cleanup.
   Hardening that fell out of B2: (a) `_scratch_output_node` multi-sink
   ambiguity now prefers the sink with the most wired inputs, so a stray
   disconnected probe node cannot hijack the commit gates/display flag;
   (b) observed model hygiene gap (probe/default nodes left in the sandbox
   and committed) is mitigated by (a) and recorded as B6 feedback material.
   **B3–B6 COMPLETE (2026-07-27):**
   - B3: three consecutive `--sketch-only` chair runs passed — valid nonblank
     PNG, zero pre-approval writes, and the static HTML checklist
     (self-contained Three.js, no animation timers, const dimension block).
   - B4: all three cases re-ran with ZERO parameter-name errors (a hard gate
     in the journey fails the run on any "parm not found"). The brief now
     injects the catalog parm cheat-sheet and forbids stalling when the
     knowledge base is unavailable (an earlier brief revision caused the
     model to refuse building when the KB cache was empty).
   - B5: `tests/runtime/fault_injection_houdini_smoke.py` — all five fault
     classes (disconnected part, wrong size, pivot error, missing part,
     wrong color) are caught on real Houdini by the extended
     `eval/geometry_assertions.py` (`mesh_component_count`,
     `evaluate_parts`, `evaluate_color`).
   - B6: each caught fault emits a bounded structured report
     `{fault, check, location, expected, actual, hint}` designed for direct
     agent feedback.
4. **Wave C:** **C1 typed parameter expressions COMPLETE (2026-07-27):**
   `ScratchExpr` AST DTO (`num`/`ref`/`op`/`func`, function whitelist,
   depth/node caps, strict ref charset) on `set_parm` (value/expr exactly
   one); the executor resolves relative refs against the target node,
   refuses sandbox escapes, and renders Hscript `ch()` expressions on
   numeric parms only. Verified: `EXPR SMOKE OK` on Houdini 21.0.440
   (tracking cook, escape/forbidden-function refusals), offline DTO and
   executor-fake coverage, skill Stage 6 note updated. C2–C7 (components,
   parameter classification, tabs, scans, case library, eval metrics)
   remain pending; the full file-level implementation and test checklist
   for C2–C7 (subnet componentization, parameter manifest, scan harness,
   five-case library, eval metrics) is
   `docs/superpowers/plans/2026-07-27-wave-c-implementation-checklist.md` —
   executing agents start there.
   **C2–C7 implementation pass (2026-07-27, offline layer):** nested
   sandbox-relative refs now resolve across calls and nested subnets; task
   graph recording, relative-path annotations, and depth-first cleanup preserve
   full paths. `ScratchParmDeclaration` adds a fail-closed Parameter Manifest
   (64 entries / 8 KiB), optional on `ScratchCommitRequest`; the executor
   persists it in the promoted container comment and returns tab/range receipt
   metadata. Coordinator validates manifest bindings, expr-ref/dependency
   agreement, and dependency cycles. Added deterministic
   `tests/runtime/param_scan_houdini_smoke.py`, five tagged case YAMLs, and
   `eval.run_eval --report` aggregation with `null` + `not_run` for unavailable
   runtime/provider evidence. Updated component/modeling/cookbook guidance for
   subnet public-parameter + typed expr conventions.
   Offline evidence: `252 passed` across the Wave C targeted gate
   (`test_wave_c_offline.py`, scratch bridge/finalize/task graph/executor,
   geometry assertions). Hython/real-provider C2–C7 scans remain `not_run`
   pending a Houdini credentialed run; no live acceptance is claimed.
   **Deterministic Hython continuation (2026-07-27):** added
   `tests/runtime/component_houdini_smoke.py`, which builds two nested SOP
   subnets with public parameters and typed relative/derived expressions,
   commits the manifest into the container comment, verifies receipt tabs and
   ranges, checks nested annotations plus top-level OUT display/render flags,
   and deletes descendants in depth-safe order. It prints `COMPONENT SMOKE OK`
   on Houdini 21.0.440. The saved fixture
   `output/wave-c-component.hip` was scanned by
   `tests/runtime/param_scan_houdini_smoke.py`; it printed `PARAM SCAN OK` and
   produced `output/wave-c-param-scan-evidence.json`. The scanner now reports
   the bound component subnet's local output (so a larger merged component
   cannot mask a smaller parameter change), while still force-cooking the
   committed top-level OUT. Screenshot capture is intentionally recorded as
   `not_run` in this headless pass.
   Existing deterministic Hython regressions also passed:
   `task_graph_houdini_smoke.py` (`SMOKE OK`), `expr_houdini_smoke.py`
   (`EXPR SMOKE OK`), `scratch_houdini_smoke.py` (`SCRATCH SMOKE OK`),
   `fault_injection_houdini_smoke.py` (`FAULT INJECTION SMOKE OK`, five
   structured B6 reports), and `scratch_bridge_houdini_journey.py`
   (`SCRATCH BRIDGE JOURNEY OK`). Offline runtime tests: `2317 passed`
   (`uv run --frozen --extra eval pytest -q tests/runtime`); focused Wave C
   plus scratch/task/executor/geometry gate: `240 passed`. Provider credentials,
     screenshot/UI review, and real three-run acceptance remain `not_run`.
   **Public parameter interface correction (2026-07-27):** the component
   smoke now uses bounded `declare_parm` operations to put `wheel_width` and
   `frame_width` directly on their subnet parameter panels. No
   `<component>_ctrl` nodes are created or required; derived geometry refs use
   `../wheel_width` / `../frame_width`, and the manifest binds to the public
   subnet parameter. The resulting HIP was inspected headlessly: each subnet
   has a spare public parm and the committed component contains only `wheel`,
   `frame`, `assembled`, and `OUT` at the top level.
   **Natural-language HTML routing correction (2026-07-27):** a real panel
   run for `做一个程序化自行车` (`run_01f44cda63364b0e9a4cb2ccd4a316db`)
   called `write_todos` and `scene_status`, then planned a direct
   `scratch_build`; it emitted no `render_sketch`. Root cause: the Runtime
   system prompt explicitly recommended direct scratch construction for
   familiar modeling while the procedural/HTML skills were only available
   through on-demand Knowledge search and therefore were never injected.
   `BASE_PROMPT` now classifies procedural/parametric and complex multi-part
   assets into the mandatory HTML -> user review -> Houdini route, lists
   `render_sketch`/`verify_geometry`, requires the project-workflow Knowledge
   lookup, and forbids Houdini writes before a later user approval message.
   `HtmlWorkflowGuardMiddleware` independently enforces that boundary from
   checkpointed messages: `scratch_build`/`scratch_commit` return
   `workflow.html_sketch_required` before a successful render and
   `workflow.sketch_approval_required` before post-render approval. Simple,
   explicit node edits remain on the direct sandbox route. Offline Runtime
   evidence after the correction: `2324 passed`. After restarting Runtime,
   a natural-language live probe `做一个程序化自行车`
   (`run_92f90b85b180433aa9bd58730e59adb2`) completed with
   `write_todos -> search_houdini_knowledge -> render_sketch` and **zero**
   `scratch_build` calls. `render_sketch` returned a non-empty 1440×900 PNG
   and HTML artifact, and the final response stopped for user review.
5. **Manual Houdini UI:** inspect the read-only task graph block in the docked
   panel after a real modeling run.

6. **Sandbox terminal-lifecycle correction (2026-07-27, offline verified):**
   local Runtime records for `run_07f16371d5b649ba80a61bdb215d7006` and
   `run_810b1a8b83da464cb54b6c395cd3059a` exposed two coupled defects.
   `_run_guarded` called `scratch.destroy` for every terminal status, so Stop
   erased the cancelled run and a completed-but-uncommitted run erased its
   recovery sandbox immediately after reporting failure. Terminal convergence
   now preserves those sandboxes; successful commit already promotes the
   container, while destroy remains an explicit disposal capability. Task-graph
   recording now marks applied `delete_node` operations deleted and records only
   the applied prefix of partial batches. Commit metadata deterministically
   retains the 64 most recent live-node annotations and reports omitted older
   annotations as a warning instead of failing with
   `ScratchCommitRequest.annotations exceeds the maximum count`. Offline
   lifecycle/bridge/task-graph regressions pass; a fresh Runtime restart plus
   interactive Houdini Stop/commit check remains pending.

The detailed execution and evidence rules are in
`docs/superpowers/plans/2026-07-26-hython-agent-chain-roadmap.md`.

## Secret and local-state rules

- No API key is required to clone, install, run the offline suite, or execute
  deterministic Hython smokes.
- A real Provider key is required only for the opt-in Provider acceptance run.
- Copy `.env.example` to `.env` locally if desired; `.env` is ignored and must
  remain untracked.
- Runtime state (`app.sqlite*`, `checkpoints.sqlite*`, `runtime.token`,
  `runtime.json`, `runtime.lock`, logs) is disposable local state and must not
  be committed.
