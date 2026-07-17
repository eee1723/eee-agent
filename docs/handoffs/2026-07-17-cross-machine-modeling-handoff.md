# EEE Agent Cross-machine Modeling Handoff - 2026-07-17

## Resume point

- Repository: `https://github.com/eee1723/eee-agent.git`
- Development branch: `feature/runtime`
- Upstream: `origin/feature/runtime`
- Accepted implementation tip before this documentation update: `2433966`
- Houdini baseline: `21.0.440`, Python `3.11`, bundled `rpyc 4.1.0`
- Latest complete offline gate: `2171 passed, 1 skipped` (the skip is the
  optional WSL probe)

After cloning, the authoritative resume point is the tip of
`origin/feature/runtime`, including this handoff. Do not merge `main`, rewrite
the accepted branch history, or force-push as part of machine setup.

## Fresh computer recovery

Install Git, PowerShell, `uv`, and Houdini 21.0.440, then run:

```powershell
git clone https://github.com/eee1723/eee-agent.git E:\eee-agent
Set-Location E:\eee-agent
git switch --track origin/feature/runtime

$Candidates = @(
    'C:\Program Files\Side Effects Software\Houdini 21.0.440',
    'D:\houdini'
)
$HFS = $Candidates |
    Where-Object { Test-Path (Join-Path $_ 'bin\hython.exe') } |
    Select-Object -First 1
if (-not $HFS) { throw 'Set $HFS to the local Houdini 21.0.440 install.' }

$HoudiniPython = Join-Path $HFS 'python311\python.exe'
uv sync --frozen --extra eval --python $HoudiniPython
uv lock --check
Copy-Item .env.example .env
```

Fill the selected provider credential in `.env`; never commit it. Do not copy
`.venv`, `.env`, Runtime SQLite files, bearer tokens, Houdini preferences, or
cached discovery state from the old computer. They are machine-local. The full
restore procedure and exact checks are in `SETUP.md`.

Verify the restored checkout before changing code:

```powershell
git status --short --branch
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
```

Install the Houdini package and launch the current Runtime UI when interactive
testing resumes:

```powershell
& (Join-Path $HFS 'bin\hython.exe') houdini_side\install_menu.py
uv run --frozen --extra eval python -m eee_agent.runtime serve
```

Restart Houdini after installing the package, then choose
**EEE Agent -> Open Runtime Control**. This menu action starts the Secure Bridge
and opens the panel. See `houdini_side/README_INSTALL.md` for manual controls.

## Product and permission model

The absence of model-facing `create_node`, `connect_nodes`, `set_parms`, and
raw Apply tools is intentional. The model can query trusted scene facts and
submit a strict `propose_modeling` payload. Developer-owned code validates and
compiles that payload into catalog-gated typed operations. Only an exact,
persisted user approval may trigger the internal single-flight Apply path.

An empty scene does not require the user to create or bind a Workspace. The
first approved modeling request bootstraps an EEE-owned `geo` root and the SOP
graph in one transaction, then persists the Workspace only after a reconciled
successful receipt. Manual **Create/Bind Workspace** remains a diagnostic path
for adopting an already EEE-owned graph. Therefore the message
`The selection does not contain trusted EEE-owned nodes` is correct for an
ordinary manually created Houdini selection; it is not the normal new-project
workflow.

The UI is converging on one normal MODEL/REVIEW flow, not a final product made
of many permanent control panels. Product mode currently defaults to:

```text
describe model -> plan/progress -> review -> Approve and build
               -> apply/validate -> result or actionable recovery
```

Scene, Workspace, IDs, revisions, receipts, and recovery controls are hidden
behind **Details** for diagnostics. The first product-mode slice is implemented,
but final Houdini GUI acceptance for focus, Chinese IME, narrow dock layout,
mouse interaction, reconnection, and visual hierarchy is deliberately still
pending.

## Accepted implementation state

### Trusted modeling path

- Strict Brief/Spec contracts reject unknown fields and model-supplied trusted
  identities.
- A deterministic compiler is the only path from Spec to typed ChangeSet.
- Empty-scene bootstrap creates ownership mirrors and persists the manifest
  atomically only after successful reconciliation.
- Public approval is bounded; Apply remains internal and uses the accepted
  FIFO/main-thread transaction, receipt, idempotency, rollback, and restart
  recovery boundaries.
- Pre-Apply SpecContract/Graph validation and post-Apply Cook/Geometry
  validation produce durable bounded evidence.
- Repair budgets are explicit and capped at two attempts per stage.
- Deterministic parameter-sensitivity sampling restores the exact baseline;
  the disposable hython smoke verifies that restoration.
- Golden Case semantic validation checks expected names, Houdini types, and
  terminal nodes.

### Catalog and Golden Cases

The trusted catalog currently covers `geo`, `box`, `grid`, `merge`, `null`,
`xform`, `normal`, `subdivide`, `polyextrude::2.0`, `fuse::2.0`, `line`,
`resample`, `sweep::2.0`, `copytopoints::2.0`, and `boolean::2.0`. Models use
safe aliases; only the catalog resolves versioned internal Houdini types.

Eight deterministic cases replay in Houdini 21.0.440: box transform, grid
transform, merged sources, subdivided surface, extruded/fused grid, copied
boxes, swept lines, and boolean union.

### Main code map

- Modeling DTOs and trust fields: `eee_agent/modeling/contracts.py`
- Catalog and safe aliases: `eee_agent/modeling/catalog.py`
- Deterministic compiler: `eee_agent/modeling/compiler.py`
- Empty-scene ownership bootstrap: `eee_agent/modeling/bootstrap.py`
- Proposal parsing/coordinator: `eee_agent/modeling/proposal.py`
- Validation and repair budget: `eee_agent/modeling/validation.py`
- Golden Case definitions: `eee_agent/modeling/golden_cases.py`
- Durable ChangeSet/Workspace transitions:
  `eee_agent/changesets/repository.py`, `eee_agent/changesets/service.py`
- Trusted typed Bridge integration:
  `eee_agent/houdini_bridge/changeset_provider.py`
- Runtime orchestration: `eee_agent/runtime/service.py`
- Houdini product/diagnostic panel: `houdini_side/runtime_panel.py`
- Houdini transactional executor: `houdini_side/changeset_executor.py`

## Verification evidence

The last accepted complete gate was:

```text
uv run --frozen --extra eval pytest -q
2171 passed, 1 skipped in 105.26s

uv lock --check
passed

python -m compileall -q eee_agent houdini_side tests
passed

git diff --check
passed
```

Disposable Houdini tests passed on
`C:\Program Files\Side Effects Software\Houdini 21.0.440`:

```powershell
& "$HFS\bin\hython.exe" -u tests\modeling\bootstrap_houdini_smoke.py
& "$HFS\bin\hython.exe" -u tests\modeling\golden_cases_houdini_smoke.py
```

Bootstrap evidence covers Applied, Cook/Geometry, parameter restoration,
ownership metadata, manifest derivation, AlreadyApplied, and rollback. Golden
Case evidence covers all eight graphs plus semantics, cooking, geometry, and
manifest checks. Houdini can print a harmless Qt
`QObject::startTimer: Timers can only be used with threads started with QThread`
warning after a successful hython run.

## Remaining work, in order

1. **Task 18-F: production transactional sensitivity Bridge.** Move parameter
   sampling from deterministic validation/disposable smoke into a typed,
   main-thread Bridge operation that always restores the exact value and fails
   closed on uncertainty. Add interruption, cook-failure, restore-failure,
   stale-scene, restart, and evidence tests.
2. **Task 19-A: Artifact evidence.** Add content-addressed metadata, bounded
   retention, deterministic capture/framing, byte identity between displayed
   and vision-consumed images, and an Artifact inspector section.
3. **Task 18-G: richer asset-level Golden Cases.** Expand only in small
   hython-verified catalog batches; keep file/source/expression/Python/VEX
   parameters excluded until a separate source policy is accepted.
4. **Task 19-B/19-C: vision routing and delivery evaluation.** Vision is
   advisory and may never override a deterministic failure. Preserve explicit
   unavailable/waiver evidence.
5. **Task 18-H: final GUI pass.** Finish validation/artifact result surfaces,
   then ask the user to perform the deferred real-Houdini focus, Chinese IME,
   button, reconnect, layout, and complete journey acceptance.

The immediate autonomous slice is item 1. Interactive Houdini testing is not
required until the implementation and hython gates above are green.

## Boundaries that must not regress

- Never expose raw Houdini writes, Apply, shell, filesystem, Bridge, SQLite, or
  source execution to the model or public Runtime protocol.
- Never infer ownership from a plain Houdini selection.
- Never persist a provisional bootstrap Workspace before successful receipt
  reconciliation.
- Never replay an uncertain write during restart recovery.
- Never let disconnect/cancellation cancel an already accepted Apply.
- Never allow vision or language output to override deterministic validation.
- Preserve Task 17 reconnect and Chinese IME fixes while reshaping the UI.

## Suggested first-session prompt

```text
Read CLAUDE.md and docs/handoffs/2026-07-17-cross-machine-modeling-handoff.md.
Verify feature/runtime is clean and matches origin. Continue with Task 18-F's
production transactional parameter-sensitivity Bridge, preserving all trust,
single-FIFO, rollback, receipt, restart-recovery, and no-public-write-tool
boundaries. Use offline tests and disposable Houdini 21.0.440 hython; defer only
the documented final GUI acceptance.
```
