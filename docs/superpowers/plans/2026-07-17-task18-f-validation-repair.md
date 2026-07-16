# Task 18-F Validation and Repair Plan

## Goal

Add deterministic post-Apply quality gates and an auditable maximum-two-repair
loop.

## Slices

1. Frozen ValidationReport/ValidatorResult/EvidenceRef contracts and storage.
2. SpecContract and Graph validators over compiler/receipt facts.
3. Cook and Geometry validators through typed Bridge reads.
4. ParameterSensitivity validator using bounded catalog samples and exact
   scene restoration.
5. Semantic and Artifact validators with deterministic hard-failure rules.
6. Strict RepairTicket generation and RepairBudget consumption.
7. New proposal/approval/Apply per repair; no silent mutation and no spec-only
   success rewrite.
8. Golden Case replay and restart recovery.

## Acceptance

Every validator has pass/fail/unavailable/stale tests. Two failed repairs end
in an explicit exhausted result. Vision never overrides a hard validator.
Hython verifies cook, geometry, sensitivity, restoration, and failed-cook
evidence.

