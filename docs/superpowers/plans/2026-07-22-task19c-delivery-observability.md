# Task 19-C Delivery and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every contract/repository/UI change, superpowers:systematic-debugging for invariant or migration failures, and superpowers:verification-before-completion before claiming Task 19-C complete. Maintain checkbox (`- [ ]`) state while executing.

**Goal:** Produce an immutable, versioned, bounded delivery summary for every terminal modeling journey, restore it through snapshot/replay, present it as structured UI with a trusted parameter guide, and optionally export allowlisted traces to Phoenix without making local correctness depend on telemetry.

**Architecture:** Existing Run, ChangeSet, ChangeReceipt, ValidationReport event, ArtifactStore, Vision evaluation, Workspace, and recovery records remain source truth. A pure `DeliveryBuilder` validates their agreement and creates a reference-oriented `DecisionSummary`; `DeliveryRepository` persists versions idempotently; the service emits `delivery.summary_created` only after the row commits. Panel state and view models consume the same DTO. An outer `ObservabilitySink` receives normalized records after local commits and isolates every exporter failure.

**Tech Stack:** Python 3.11, SQLite migration v7, existing canonical JSON/ID/EventStore patterns, Qt-free view models plus thin PySide6, optional `arize-phoenix-otel>=0.16,<1` with auto-instrumentation disabled.

**Workspace:** `E:\eee-agent\.worktrees\runtime`, branch `feature/c-task19c-delivery`, created only from a Stage B PASS commit. Claude Code uses exact model `glm-5.2[1m]` for bounded implementation batches; Codex reviews, verifies, and commits. No implementation worker may push, merge, rebase, tag, edit `.env`, or mutate worktrees.

**Approved spec:** `docs/superpowers/specs/2026-07-22-task19c-delivery-observability-design.md`

**Important existing debt:** `eee_agent.vision.evaluation.DeliveryEvaluation` currently copies bounded brief/spec text into a durable event. C treats that object as a compatibility input only; the new summary stores digests/references, not copies. Removing or migrating the older event shape is a separate compatibility decision and must be tracked, not silently combined with the new schema.

---

### Task 1: Audit durable source truth and lock the aggregation boundary

**Files:**

- Create: `docs/superpowers/reviews/2026-07-22-task19c-source-audit.md`
- Modify: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`
- Read only: `eee_agent/runtime/migrations.py`, `events.py`, `service.py`, `artifacts.py`
- Read only: `eee_agent/changesets/repository.py`, `contracts.py`
- Read only: `eee_agent/modeling/validation.py`, `compiler.py`, `contracts.py`
- Read only: `eee_agent/vision/evaluation.py`, `router.py`

- [ ] **Step 1: Create C branch only after B is accepted**

```powershell
git status --porcelain=v1
rg -n "Decision: PASS|Stage B: PASS" docs/superpowers/reviews/2026-07-22-stage-b-acceptance.md
git switch -c feature/c-task19c-delivery
```

Expected: a clean B PASS tip.

- [ ] **Step 2: Inventory each proposed output field against exactly one owner**

The audit table must map:

| Delivery field | Authoritative source | Reference copied |
|---|---|---|
| session/run IDs | `SessionRecord` / `RunRecord` | IDs only |
| workspace | active workspace state + manifest | ID and revision/digest |
| change | `ChangeSetRecord` | ID and digest |
| receipt | `ChangeReceipt` | status, operation count, digest, bounded error code |
| validation | `modeling.validation_completed` typed payload | report digest, complete/status |
| artifacts | `ArtifactStore` rows | ArtifactRef + current state |
| vision | `DeliveryEvaluation`/Vision durable event | status, normalized conclusion/digest |
| recovery | receipt/recovery durable state | status and required action code |
| parameter guide | approved `ProceduralSpec` assignments + trusted catalog metadata | bounded typed values only |

If two sources can disagree, identify the fail-closed rule. Never select by “latest event” without checking change ID/digest/run/session ownership.

- [ ] **Step 3: Prove restart availability for every source**

For each row, document the repository query or durable event replay path available after `RuntimeService.open()`. Flag transient-only objects. A required transient-only source is an architecture blocker until made durable; it is not filled from panel memory.

- [ ] **Step 4: Record compatibility and privacy debt**

Register at least:

- existing `DeliveryEvaluation.brief/spec` text duplication;
- whether validation event payload can be reconstructed into an exact typed report;
- missing catalog description/range metadata for a useful parameter guide;
- Phoenix optional dependency/support status (RN-011).

Commit the audit before code so later schema choices can be checked against it.

```powershell
git add docs/superpowers/reviews/2026-07-22-task19c-source-audit.md docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "docs: audit task 19-c durable source truth"
```

---

### Task 2: Define strict delivery and parameter-guide contracts

**Files:**

- Modify: `eee_agent/core/ids.py`
- Create: `eee_agent/runtime/delivery.py`
- Create: `tests/runtime/test_delivery_contracts.py`
- Create: `tests/runtime/test_delivery_decisions.py`
- Modify: `tests/core/test_ids.py`

- [ ] **Step 1: Add RED ID tests**

Add `IdKind.DELIVERY = "dly"` and tests requiring `new_id(IdKind.DELIVERY)` to produce `dly_` plus exactly 32 lowercase hex characters; wrong prefixes and malformed payloads must fail through `require_id`.

- [ ] **Step 2: Write RED contract tests for exact keys, bounds, and immutability**

Define and test these public contracts:

```python
class DeliveryStatus(StrEnum):
    ACCEPTED = "accepted"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class ParameterGuideEntry:
    parameter_id: str          # node_key.parm_name, bounded stable identifier
    display_name: str          # trusted catalog label
    owner_node_id: str         # created node identity, never arbitrary path
    value_type: str            # bool|int|float|string|tuple
    current_value: object      # strict bounded JSON scalar/tuple
    minimum: int | float | None
    maximum: int | float | None
    choices: tuple[str, ...]
    description: str
    sensitivity_validated: bool


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    delivery_id: str
    version: int
    session_id: str
    run_id: str
    workspace_id: str | None
    workspace_revision: str | None
    change_id: str
    changeset_digest: str
    status: DeliveryStatus
    reason_code: str
    reason: str
    validation_status: str
    validation_report_digest: str | None
    receipt_status: str
    receipt_digest: str
    applied_operation_count: int
    receipt_error_code: str | None
    artifacts: tuple[DeliveryArtifact, ...]
    vision_status: str
    vision_summary: str
    parameter_guide: tuple[ParameterGuideEntry, ...]
    recovery_status: str
    required_action: str
    source_digest: str
    truncated_parameter_count: int
    created_at: datetime
    schema_version: int = 1
```

Add a small `DeliveryArtifact` contract containing only artifact ID, SHA-256, media type, verified state, and size; omit arbitrary filesystem paths from the summary. `to_dict`/`from_dict` must require exact keys, UTC timestamps, version 1, exact IDs, lowercase SHA-256, finite numbers, maximum 16 Artifacts, maximum 64 guide entries, maximum 16 choices, 120-char names, 512-char descriptions/reasons, and deep immutability via existing freeze/thaw helpers.

- [ ] **Step 3: Write RED decision-matrix tests**

Introduce exact typed `DeliverySources` used only by the builder. Cover all precedence boundaries:

1. contradictory session/run/change/digest ownership raises `DeliveryInvariantError`;
2. `Partial` or `CriticalRecovery`, or unresolved recovery -> `RECOVERY_REQUIRED`;
3. `RolledBack` -> `FAILED`;
4. deterministic validation not complete/passed -> `FAILED`;
5. missing required or non-available Artifact -> `INCOMPLETE`;
6. Applied/AlreadyApplied + complete validation + required Artifact available -> `ACCEPTED`;
7. Vision FAILED/UNAVAILABLE remains explicit but does not by itself demote an otherwise accepted local delivery;
8. Vision can never promote deterministic failure;
9. identical sources produce identical `source_digest` even if mapping input order differs;
10. parameter truncation is deterministic and records the omitted count.

- [ ] **Step 4: Implement the pure `DeliveryBuilder`**

`DeliveryBuilder.build(sources, *, prior_version=0, clock=...)` validates all cross-record identities first, sorts artifacts by artifact ID and parameters by stable parameter ID, computes `source_digest` over reference facts with `canonical_json_dumps`, and returns version `prior_version + 1`. It generates a new delivery ID but idempotency is enforced by repository uniqueness on `(change_id, source_digest)`.

Use stable reason/action codes such as:

- `delivery.accepted` / `none`;
- `delivery.validation_failed` / `review_validation`;
- `delivery.artifact_missing` / `retry_capture`;
- `delivery.rolled_back` / `review_apply_error`;
- `delivery.recovery_required` / `restart_houdini_and_reconcile`.

Messages are constant/bounded; do not copy exception text or prompt content.

- [ ] **Step 5: Run and commit the contract slice**

```powershell
uv run --frozen --extra eval pytest -q tests/core/test_ids.py tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py
uv run --frozen ruff check eee_agent/core/ids.py eee_agent/runtime/delivery.py tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py
git add eee_agent/core/ids.py eee_agent/runtime/delivery.py tests/core/test_ids.py tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py
git diff --cached --check
git commit -m "feat: define immutable task 19-c delivery decisions"
```

---

### Task 3: Add trusted parameter metadata and derive the guide

**Files:**

- Modify: `eee_agent/modeling/compiler.py` (`ParmDefinition`)
- Modify: `eee_agent/modeling/catalog.py`
- Modify: `eee_agent/modeling/validation.py` only to expose exact sensitivity membership if needed
- Modify: `eee_agent/runtime/delivery.py`
- Modify: `tests/modeling/test_compiler.py`
- Modify: `tests/modeling/test_catalog.py` or existing catalog test module
- Modify: `tests/runtime/test_delivery_contracts.py`

- [ ] **Step 1: Write RED metadata validation tests**

Extend `ParmDefinition` backward-compatibly:

```python
@dataclass(frozen=True, slots=True)
class ParmDefinition:
    parm_name: str
    default_value: object
    label: str = ""
    description: str = ""
    minimum: int | float | None = None
    maximum: int | float | None = None
    choices: tuple[str, ...] = ()
    user_adjustable: bool = False
```

Tests require: labels <=120, descriptions <=512, finite numeric min/max of the same scalar family, min <= max, at most 16 bounded choices, choices only for string/int-style menu definitions, and `user_adjustable` exact bool. Old two-argument construction must remain valid and serialize deterministically.

- [ ] **Step 2: Add metadata only for genuinely exposed controls**

For the Houdini 21 minimal catalog, annotate a small verified subset such as box size/divisions, grid rows/columns/size, transform translate/rotate/scale, tube radius/height, and sphere radius/frequency where current catalog types and hython evidence support them. Do not mark file, code, expression, Python, VEX, hidden bookkeeping, topology-internal, or unverified parameters adjustable.

Descriptions must be authored constants in the trusted catalog, not model output. Bounds must match existing compiler type shapes and Houdini smoke evidence.

- [ ] **Step 3: Derive guide entries only from approved spec assignments**

Add a pure helper that joins `ProceduralSpec.nodes[].parameters` to `NodeCatalog.by_type[].parameters_by_name`, includes only `user_adjustable=True`, resolves owner node IDs from the approved ChangeSet's created references, and marks sensitivity membership from the deterministic sample plan. Unknown or mismatched metadata fails closed or is omitted with a counted reason; it never guesses a path.

- [ ] **Step 4: Run compiler/catalog/delivery regression**

```powershell
uv run --frozen --extra eval pytest -q tests/modeling tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py
uv run --frozen ruff check eee_agent/modeling eee_agent/runtime/delivery.py tests/modeling tests/runtime/test_delivery_contracts.py
git add eee_agent/modeling eee_agent/runtime/delivery.py tests/modeling tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py
git diff --cached --check
git commit -m "feat: derive delivery parameter guide from trusted catalog"
```

---

### Task 4: Persist versioned summaries with atomic idempotency

**Files:**

- Modify: `eee_agent/runtime/migrations.py`
- Create: `eee_agent/runtime/deliveries.py`
- Modify: `tests/runtime/test_database.py`
- Create: `tests/runtime/test_delivery_repository.py`

- [ ] **Step 1: Write RED migration v7 tests**

Require `SCHEMA_VERSION == 7`, unchanged checksums for migrations 1-6, fresh/open upgrade from v6, rollback on failure, exact columns/indexes/check constraints, and rejection of a tampered v7 checksum.

Add additive SQL only:

```sql
CREATE TABLE delivery_summaries (
    delivery_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    change_id TEXT NOT NULL REFERENCES changesets(change_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'accepted', 'incomplete', 'failed', 'recovery_required'
    )),
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    UNIQUE(change_id, version),
    UNIQUE(change_id, source_digest)
);
CREATE INDEX deliveries_by_session_created
    ON delivery_summaries(session_id, created_at, delivery_id);
CREATE INDEX deliveries_by_run_version
    ON delivery_summaries(run_id, version);
```

- [ ] **Step 2: Write RED repository tests**

`DeliveryRepository` must support:

- `put(summary) -> (stored_summary, created: bool)`;
- same change/source digest returns the existing row without a new version;
- a changed source digest requires next exact version;
- concurrent identical puts create one row;
- concurrent differing puts serialize into adjacent versions or reject stale requested versions deterministically;
- `get(delivery_id)`, `latest_for_change(change_id)`, `list_for_session(session_id, limit<=100)`, `list_for_run(run_id)`;
- canonical JSON round trip and immutable returned DTO;
- FK/cross-identity mismatch fails before insert;
- no silent overwrite/update method exists.

- [ ] **Step 3: Implement repository using one write transaction**

Validate/freeze/canonicalize before the first await. Inside `write_transaction`, verify the referenced Run and ChangeSet belong to the same Session and to each other, query an existing `(change_id, source_digest)`, allocate `MAX(version)+1`, and insert. On uniqueness races, re-read only when the digest matches; otherwise propagate a bounded invariant error.

- [ ] **Step 4: Run migration/repository suites and commit**

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_database.py tests/runtime/test_delivery_repository.py
uv run --frozen ruff check eee_agent/runtime/migrations.py eee_agent/runtime/deliveries.py tests/runtime/test_database.py tests/runtime/test_delivery_repository.py
git add eee_agent/runtime/migrations.py eee_agent/runtime/deliveries.py tests/runtime/test_database.py tests/runtime/test_delivery_repository.py
git diff --cached --check
git commit -m "feat: persist versioned delivery summaries"
```

---

### Task 5: Aggregate after durable sources and recover without side effects

**Files:**

- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/runtime/events.py`
- Modify: `eee_agent/runtime/models.py` only if a snapshot record type belongs there
- Modify: `eee_agent/runtime/server.py`
- Modify: `eee_agent/runtime/protocol.py` only if exact snapshot/event shape tests require it
- Create: `tests/runtime/test_delivery_service.py`
- Modify: `tests/runtime/test_events_store.py`
- Modify: `tests/runtime/test_server.py`
- Modify: `tests/runtime/test_changeset_recovery.py`

- [ ] **Step 1: Write RED service decision and event tests**

Test one complete flow and every incomplete/failure boundary. A successful path must persist the row, then append exactly one durable `delivery.summary_created` event with the full bounded summary DTO. Repeating aggregation must return the same row and emit no duplicate event. If event append fails after row commit, reconciliation on restart must emit the missing event without Apply/capture/Vision replay.

- [ ] **Step 2: Add typed source loading**

Implement private service helpers that load exact change, receipt, validation event keyed by change/digest, current ArtifactStore rows referenced by that event, Vision evaluation keyed by the same run/change/artifact, active workspace facts, recovery status, approved spec/catalog, and prior delivery. Reject contradictions with a bounded internal invariant event/finding; do not continue with partial mismatched facts.

Because current validation is durable in events, add an exact `ValidationReport.from_dict()` if absent and validate exact keys before using it. Do not treat a loose `complete=true` flag alone as the complete report.

- [ ] **Step 3: Choose one terminal aggregation trigger**

Call `_ensure_delivery_summary(change_id)` after the existing post-Apply validation/capture/Vision pipeline reaches its local terminal boundary, and from `_reconcile()` for applied/recovery records without a corresponding delivery. The method may read or write delivery/event rows only. It must never call Bridge Apply, capture, provider evaluate, model runner, or scene mutation.

- [ ] **Step 4: Extend snapshot consistently**

Add `deliveries: tuple[DecisionSummary, ...]` to `SessionSnapshotData` and `SessionSnapshot`, reading latest summaries for the included Runs inside the same snapshot transaction/sequence boundary. Update `_snapshot_to_dict` to emit a `deliveries` list. Backward-compatible panel parsing treats a missing list as empty; Runtime contract tests require exact new-server output.

- [ ] **Step 5: Test crash windows and no-side-effect restart**

Inject failures at: source load, build, row insert, event append, callback notify, service shutdown. After reopen, assert coherent latest summary and at most one event for its delivery ID/source digest. Spy Bridge/provider/capture calls and require zero aggregation-induced calls.

- [ ] **Step 6: Run focused service/protocol/recovery suites and commit**

```powershell
uv run --frozen --extra eval pytest -q `
  tests/runtime/test_delivery_service.py `
  tests/runtime/test_delivery_repository.py `
  tests/runtime/test_events_store.py `
  tests/runtime/test_server.py `
  tests/runtime/test_changeset_recovery.py
git add eee_agent/runtime tests/runtime/test_delivery_service.py tests/runtime/test_events_store.py tests/runtime/test_server.py tests/runtime/test_changeset_recovery.py
git diff --cached --check
git commit -m "feat: aggregate and replay durable delivery summaries"
```

---

### Task 6: Project and render delivery in the three-pane panel

**Files:**

- Modify: `eee_agent/panel/runtime_state.py`
- Modify: `houdini_side/runtime_panel/view_models.py`
- Modify: `houdini_side/runtime_panel/main_window.py`
- Modify: `houdini_side/runtime_panel/inspector.py`
- Modify: `tests/panel/test_runtime_state.py`
- Modify: `tests/panel/test_runtime_panel_view_models.py`
- Modify: `tests/panel/test_runtime_panel_sources.py`
- Modify: behavior tests for replay/session switching

- [ ] **Step 1: Write RED state projection tests**

Panel state must accept `delivery.summary_created`, validate the exact bounded DTO shape, keep only the latest version per change ID, expose deliveries in `snapshot()`, seed them from `session.snapshot`, ignore exact duplicate delivery IDs, and reject version regression/conflicting same-version payloads without corrupting current state.

- [ ] **Step 2: Implement Qt-free view models**

Add:

- `DeliveryCardView`: short ID, status/tone, validation, receipt, artifact count/state, Vision state, next action;
- `DeliveryView`: all structured reference fields, recovery, evidence rows, tuple of `ParameterGuideRow`;
- `delivery_card(summary) -> MessageItem` for one compact conversation card;
- `delivery_view(summary) -> DeliveryView | None` for Inspector.

Map accepted -> ok, incomplete -> warn, failed/recovery_required -> error. Never use `gate`/approval amber. Bound every string and collection; never `str(mapping)`.

- [ ] **Step 3: Render the compact card exactly once**

`main_window.py` listens to projected delivery changes and appends one delivery card keyed by delivery ID. History/session activation rebuilds from snapshot deliveries in chronological/version order. Repeated snapshots/reconnects do not duplicate cards. Delivery cards are distinct from the Run terminal output/failure card and Apply outcome.

- [ ] **Step 4: Add structured Inspector sections**

Extend the Run view or add a DELIVERY tab according to the existing three-pane hierarchy. Render status/reason, Validation, Receipt, Artifacts, Vision, Recovery/next action, and a parameter table with name/current/range-or-choices/sensitivity. Full bounded identifiers are selectable; visual short IDs remain concise. No raw JSON/text wall.

- [ ] **Step 5: Run panel suites and commit**

```powershell
uv run --frozen --extra eval pytest -q tests/panel
uv run --frozen ruff check eee_agent/panel houdini_side/runtime_panel tests/panel
uv run --frozen mypy eee_agent/panel
git add eee_agent/panel houdini_side/runtime_panel tests/panel
git diff --cached --check
git commit -m "feat: present structured task 19-c delivery in panel"
```

---

### Task 7: Add a fail-isolated observability core

**Files:**

- Modify: `eee_agent/config.py`
- Create: `eee_agent/observability/__init__.py`
- Create: `eee_agent/observability/contracts.py`
- Create: `eee_agent/observability/service.py`
- Create: `tests/observability/test_contracts.py`
- Create: `tests/observability/test_service.py`

- [ ] **Step 1: Write RED allowlist and redaction tests**

Define a strict `TelemetryRecord` with only:

```python
event_name: str
timestamp: datetime
session_ref: str       # one-way digest/prefix, not raw ID
run_ref: str | None
duration_ms: float | None
status: str
tool_name: str | None
validator_name: str | None
error_code: str | None
error_category: str | None
source_digest: str | None
delivery_status: str | None
receipt_status: str | None
artifact_status: str | None
vision_status: str | None
input_tokens: int | None
output_tokens: int | None
```

Exact-key parsing rejects prompt/message/content/path/credential/raw payload fields. Bounds: 128-char names/codes, finite nonnegative durations, nonnegative token counts, lowercase digests. Add `normalize_event(record)` from allowlisted Runtime event types; unknown events return `None`.

- [ ] **Step 2: Define the sink and isolation wrapper**

```python
class ObservabilitySink(Protocol):
    async def emit(self, record: TelemetryRecord) -> None: ...
    async def close(self) -> None: ...


class NullObservabilitySink:
    async def emit(self, record: TelemetryRecord) -> None: ...
    async def close(self) -> None: ...
```

`ObservabilityService.emit()` validates first, calls the sink behind a bounded timeout, catches exporter/network/serialization errors, increments a local bounded failure counter, and never raises into Run/Apply/delivery. Diagnostics go through a local logger that is not routed back to the sink, preventing recursion.

- [ ] **Step 3: Add explicit disabled-by-default configuration**

Add `observability_config()` returning `None` unless `EEE_OBSERVABILITY_PROVIDER=phoenix`. Validate provider exact value, endpoint scheme (`http`/`https`, no embedded credentials), bounded project name, timeout, batch flag. Do not read or echo API-key content; Phoenix SDK reads its own `PHOENIX_API_KEY` environment variable only after explicit enablement.

- [ ] **Step 4: Prove local behavior is identical on sink failure**

Service tests run the same delivery operation with Null, recording fake, raising fake, hanging fake, and close-failing fake sinks. Compare stored Runs, receipts, validation, Artifacts, Vision, and deliveries byte-for-byte. Only the observability failure counter may differ.

- [ ] **Step 5: Commit the core without Phoenix dependency**

```powershell
uv run --frozen --extra eval pytest -q tests/observability
uv run --frozen ruff check eee_agent/observability eee_agent/config.py tests/observability
git add eee_agent/observability eee_agent/config.py tests/observability
git diff --cached --check
git commit -m "feat: add fail-isolated observability boundary"
```

---

### Task 8: Add the optional Phoenix adapter and real export smoke

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `eee_agent/observability/phoenix.py`
- Modify: `eee_agent/runtime/__main__.py`
- Create: `tests/observability/test_phoenix_adapter.py`
- Create: `tests/observability/phoenix_smoke.py`
- Modify: `.env.example`
- Update: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md` (RN-011)

- [ ] **Step 1: Add the supported optional dependency with a reproducible lock**

```powershell
uv add --optional observability "arize-phoenix-otel>=0.16,<1"
uv lock --check
```

The lower bound is required for current `phoenix.otel` imports. Review the resolved exact versions and transitive dependency diff before accepting. Core/offline installation without `--extra observability` must still import and run.

- [ ] **Step 2: Write RED lazy-import tests**

Prove:

- disabled startup never imports `phoenix`;
- enabled but dependency-missing returns a bounded configuration/startup diagnostic without breaking Runtime startup, unless product policy explicitly chooses fail-fast for a misconfigured opt-in;
- adapter calls `phoenix.otel.register(project_name=..., auto_instrument=False, batch=True, endpoint=...)`;
- no LangChain/OpenAI auto-instrumentation captures prompts;
- spans contain only `TelemetryRecord` allowlisted attributes;
- `close()` calls provider shutdown/flush with timeout;
- emit/shutdown errors are absorbed by `ObservabilityService`.

- [ ] **Step 3: Implement manual spans only**

Lazy import inside `PhoenixObservabilitySink.__init__` or factory. Register Phoenix with `auto_instrument=False`; acquire a tracer; for each record create one span named `eee_agent.<event_name>`, set only normalized scalar attributes under `eee.*`, set status based on normalized status, and end immediately. Never attach event payloads, exceptions, stack traces, prompts, file paths, model response, or image data.

- [ ] **Step 4: Wire the sink after local service construction**

`runtime.__main__` builds Null when disabled and Phoenix only when explicitly configured, injects the wrapper into `RuntimeService`, and closes it after service/local resources in a bounded best-effort step. Runtime emits normalized records only after durable commits/notifications; exporter output cannot participate in a database transaction.

- [ ] **Step 5: Add safe example variables**

```dotenv
# EEE_OBSERVABILITY_PROVIDER=phoenix
# PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006
# PHOENIX_PROJECT_NAME=eee-agent
# PHOENIX_API_KEY=configure-locally-never-commit
```

The actual `.env.example` must not contain a credential-shaped value; use an empty/commented sentinel and explanatory comment.

- [ ] **Step 6: Run offline adapter gates**

```powershell
uv run --frozen --extra eval pytest -q tests/observability
uv run --frozen --extra eval pytest -q tests/runtime/test_runtime_cli.py tests/runtime/test_delivery_service.py
uv run --frozen ruff check eee_agent/observability eee_agent/runtime/__main__.py tests/observability
uv run --frozen --extra observability python -c "from eee_agent.observability.phoenix import PhoenixObservabilitySink; print('phoenix adapter import ok')"
```

- [ ] **Step 7: Run an opt-in real Phoenix smoke**

Against an explicitly supplied local/self-hosted/Cloud endpoint, `tests/observability/phoenix_smoke.py` exports one synthetic allowlisted delivery span, calls shutdown, and writes a bounded local evidence JSON containing endpoint class (not full credential-bearing URL), project, span name, export status, timestamp, and record digest. Verify the span in Phoenix without exposing secrets. If no endpoint is available, record observability adapter acceptance as NOT RUN; do not claim supported readiness.

- [ ] **Step 8: Commit dependency, adapter, and support evidence**

```powershell
git add pyproject.toml uv.lock .env.example eee_agent/observability/phoenix.py eee_agent/runtime/__main__.py tests/observability docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "feat: export allowlisted delivery traces to optional phoenix"
```

---

### Task 9: End-to-end Task 19-C acceptance

**Files:**

- Create: `docs/superpowers/reviews/2026-07-22-task19c-acceptance.md`
- Update: `docs/superpowers/reviews/2026-07-22-runtime-next-findings.md`
- Update: `README.md` and `CLAUDE.md` only for capabilities actually accepted

- [ ] **Step 1: Run complete fresh gates**

```powershell
uv lock --check
uv run --frozen ruff check .
uv run --frozen mypy eee_agent/panel eee_agent/runtime eee_agent/vision eee_agent/observability
python -m compileall -q eee_agent houdini_side tests
uv run --frozen --extra eval pytest -q
uv run --frozen --extra eval --extra observability pytest -q tests/observability tests/runtime/test_delivery_contracts.py tests/runtime/test_delivery_decisions.py tests/runtime/test_delivery_repository.py tests/runtime/test_delivery_service.py
git diff --check main...HEAD
```

- [ ] **Step 2: Run real Houdini/provider delivery journey**

Extend the accepted Stage B journey to require one persisted `DecisionSummary`, `delivery.summary_created`, restart snapshot recovery, structured panel card/Inspector display, parameter guide from trusted catalog, and no replay of Apply/capture/Vision. Use the same Artifact ID/digest across Validation, Vision, and delivery evidence.

- [ ] **Step 3: Verify telemetry on/off equivalence**

Run the journey once with observability disabled and once with the real accepted Phoenix endpoint (or a local recording sink for deterministic comparison). Compare the canonical local delivery summary and all source records; they must be identical except IDs/timestamps deliberately generated per run. Normalize those fields before comparison and record the digest.

- [ ] **Step 4: Perform privacy and architecture review**

Search committed code/evidence:

```powershell
rg -n "brief|prompt|message_for_user|technical_detail_ref|traceback|relative_path|image_url|base64|API_KEY|TOKEN|PASSWORD" eee_agent/observability eee_agent/runtime/delivery.py eee_agent/runtime/deliveries.py docs/superpowers/reviews/2026-07-22-task19c-acceptance.md
```

Every match must be explained as validation/redaction code, safe schema documentation, or removed. Review no duplicate source truth, no delivery/telemetry dependency cycle, immutable versioning, conflict fail-closed behavior, and exporter isolation.

- [ ] **Step 5: Record the completion decision**

The acceptance report includes exact commit, migration version/checksum, test counts/warnings/skips, real Houdini/provider evidence digest, GUI results, Phoenix result (`PASS` or `NOT RUN`), unresolved finding IDs, and separate decisions:

- local Task 19-C delivery: PASS/FAIL;
- Phoenix supported adapter: PASS/NOT READY.

Local delivery may pass if Phoenix is unavailable; documentation may call Phoenix supported only after the real export smoke passes.

- [ ] **Step 6: Commit final docs and leave a clean branch**

```powershell
git add README.md CLAUDE.md docs/superpowers/reviews/2026-07-22-task19c-acceptance.md docs/superpowers/reviews/2026-07-22-runtime-next-findings.md
git diff --cached --check
git commit -m "docs: record task 19-c delivery acceptance"
git status --short --branch
```

Do not merge, push, tag, delete branches, or remove the fixed Runtime worktree without separate user authorization.
