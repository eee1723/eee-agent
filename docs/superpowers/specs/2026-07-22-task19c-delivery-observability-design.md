# Task 19-C Delivery and Observability Design

Date: 2026-07-22
Stage: C
Dependency: accepted Stage B

## Goal

Give each modeling journey a durable, bounded, user-readable delivery package
that explains what was requested, what changed, what was validated, what was
captured, how the result can be adjusted, and whether recovery action is
needed. Add optional observability export without making local correctness
depend on Phoenix or LangSmith.

## Non-goals

- Creating a second source of truth beside existing Run, ChangeSet,
  ValidationReport, ChangeReceipt, Artifact, Vision, and recovery records.
- Persisting raw model responses, prompts, secrets, tracebacks, or HOM objects.
- Allowing observability services to influence Apply or acceptance decisions.
- Adding remote telemetry by default.
- Replacing the structured three-pane UI with JSON or text dumps.

## Delivery aggregate

The Runtime builds a `DecisionSummary` from already durable, trusted records.
It references rather than duplicates full evidence and contains bounded fields
for:

- Session, Run, Workspace, and ChangeSet identities;
- final delivery status and concise reason;
- deterministic validation status and report reference;
- ChangeReceipt status, operation count, and bounded error classification;
- Artifact references and verified availability;
- advisory Vision status and normalized conclusion;
- parameter guide entries for supported exposed controls;
- recovery state and required user action;
- timestamps and schema version.

The aggregate is immutable after the delivery terminal boundary. If later
reconciliation changes artifact availability or recovery state, the Runtime
emits a new versioned delivery record rather than rewriting historical
evidence invisibly.

## Decision rules

- Deterministic validation failure always prevents accepted delivery.
- Vision may add advisory evidence but cannot override deterministic status.
- Applied/AlreadyApplied receipts with passing validation may be accepted.
- RolledBack, Partial, CriticalRecovery, missing required Artifact evidence, or
  unresolved recovery cannot be presented as successful delivery.
- Vision unavailable does not by itself fail a deterministically valid local
  delivery, but the omission is explicit.
- Conflicting source records fail aggregate construction rather than choosing
  a convenient interpretation.

## Parameter guide

The guide is derived from trusted modeling Spec/catalog/Workspace facts, not
from free-form model prose. Each entry is bounded and contains:

- user-facing name and stable parameter identity;
- owning component/node reference in trusted form;
- value type, current value, and accepted range or choices;
- short semantic description from approved catalog metadata;
- whether changing it participates in deterministic sensitivity validation.

Unsupported, hidden, source-code, file, expression, Python, and VEX parameters
remain absent unless separately authorized by policy.

## Persistence and events

Delivery summaries use a versioned DTO and durable repository storage. The
Runtime emits one bounded delivery event after source records reach a coherent
terminal state. Session snapshot/replay can recover the latest version without
recomputing from transient panel state.

Construction is idempotent for the same source digests. A restart may resume
aggregation from durable inputs but must not replay Apply, capture, or provider
side effects.

## Panel presentation

The conversation receives a compact delivery card showing status, validation,
receipt, Artifact/Vision availability, and the most important next action. The
Inspector exposes structured sections for full bounded evidence and the
parameter guide.

The presentation reuses current theme tokens and view-model boundaries:

- normal/ok/error/warn tones remain semantic;
- approval amber is not reused for ordinary delivery status;
- identifiers are shortened visually while full bounded values remain
  available in the Inspector;
- mappings are normalized into rows/cards rather than stringified.

## Optional observability adapters

Observability is an outer adapter over normalized Runtime events and delivery
records. The core imports no Phoenix or LangSmith package at module import
time. Adapters are enabled explicitly and initialized lazily.

Exported data is allowlisted:

- event type, duration, normalized status, tool name, validator name;
- bounded error code/category without raw exception text;
- source/evidence digests and redacted identifiers;
- delivery/receipt/artifact/vision state;
- token/usage metadata already approved for telemetry.

Prompts, credentials, raw provider responses, arbitrary file paths, scene
parameter values marked sensitive, and unbounded tool payloads are excluded.

Adapter initialization, network, serialization, rate-limit, or shutdown
failure is isolated. It may produce a local diagnostic but cannot fail Run,
Apply, validation, recovery, or delivery persistence.

Phoenix becomes a declared optional dependency group if it is accepted as a
supported adapter. Otherwise documentation must identify manual installation
as unsupported development setup. The project must not claim a one-click
supported feature whose dependencies are absent from the lockfile.

## Error handling

- Missing mandatory source evidence produces an explicit incomplete/failed
  summary, not an exception that hides the journey.
- Contradictory durable states raise an internal invariant finding and retain
  all source evidence for diagnosis.
- Oversized guide/evidence collections are deterministically truncated with
  counts and truncation flags.
- Broken Artifact references cannot be exported as successful evidence.
- Adapter failures are bounded and non-recursive; logging an exporter failure
  must not call the failing exporter again.

## Tests

### Contract and decision tests

- strict schema, versioning, bounds, and immutability;
- every receipt/validation/Vision/recovery combination at decision boundaries;
- deterministic failure precedence;
- conflict rejection and truncation metadata;
- parameter guide policy exclusion.

### Repository and replay tests

- durable round trip and migration;
- idempotent construction;
- restart recovery without side-effect replay;
- Session snapshot and event replay;
- artifact reconciliation producing a new summary version.

### Panel tests

- compact delivery card and structured Inspector sections;
- long and partial evidence;
- no raw mapping/text wall;
- correct semantic tones and no approval-color misuse;
- reconnection and historical display.

### Adapter tests

- disabled/missing dependency path;
- allowlisted serialization and redaction;
- network/rate-limit/shutdown failure isolation;
- local delivery remains identical with adapters on or off.

## Acceptance

Stage C requires fresh full gates, focused delivery/replay tests, and a
provider/Houdini journey that produces a coherent user-visible delivery
package. Observability is accepted independently: local Task 19-C delivery may
ship without an external adapter, but supported-adapter claims require a real
export smoke.

## Discovered-work policy

The implementation must audit existing durable records before adding new
storage or events. Duplicate truth, ambiguous terminal states, or a dependency
cycle between delivery and observability is an architectural finding and stops
the affected task. Adjacent correctness defects enter Stage C when they block
coherent delivery; unrelated analytics or dashboard features remain separate.
