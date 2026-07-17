# Task 18-F Validation Progress - 2026-07-17

## Implemented

- deterministic `ValidationReport` with explicit Passed/Failed/Unavailable/Stale;
- strict SpecContract and graph/checkpoint validators before proposal persistence;
- read-only typed `scene.query` provider over exact compiled node paths;
- post-Apply Cook validation for missing/unreadable SOP facts;
- terminal Geometry validation for non-empty points/primitives and bounded bbox;
- bootstrap object containers are excluded from SOP cook requirements;
- durable `modeling.validation_completed` or
  `modeling.validation_unavailable` Runtime events;
- per-stage maximum-two RepairBudget and explicit exhausted RepairTicket;
- deterministic Parameter Sensitivity判定 requiring changed sample geometry and
  exact baseline restoration;
- duplicate trusted Apply reads do not append duplicate validation events.

Validation never receives write authority. An unavailable validation read does
not replay or invalidate an already durable Apply receipt.

## Evidence

- full offline suite: `2170 passed, 1 skipped`;
- focused modeling/recovery suite: `77 passed`;
- Houdini 21.0.440 hython bootstrap smoke passed real cook, geometry facts,
  metadata, manifest, AlreadyApplied, and forced rollback cleanup;
- `uv lock --check`, compileall, and `git diff --check` passed.

## Remaining

Parameter Sensitivity still needs a production transactional sample-and-restore
Bridge operation. Semantic and Artifact validators need Golden Case/output
contracts. Those stages remain explicitly Unavailable until their typed evidence
exists.
