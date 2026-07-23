# Vision Confidence JSON Normalization Design

Date: 2026-07-23
Status: approved
Scope: Stage B Vision provider compatibility

## Problem

The real Qwen Vision provider returned a valid JSON number `0` for
`confidence`. Python decodes that value as `int`, while
`NormalizedVisualReport` currently requires an exact `float`. The raw provider
call therefore succeeds but report normalization fails.

## Decision

Keep the in-memory `NormalizedVisualReport` constructor strict: callers that
construct the contract directly must still provide an exact finite `float` in
the inclusive range `0.0..1.0`.

At the `NormalizedVisualReport.from_dict()` JSON boundary only:

- accept an exact `int` for `confidence`, excluding `bool`;
- validate the same finite/range constraints;
- convert the accepted integer to `float` before constructing the contract;
- continue rejecting booleans, strings, non-finite values, and values outside
  `0..1`.

The Vision provider adapter remains unchanged and continues to return the
bounded raw mapping. The normalization contract remains the single authority
for interpreting external provider output.

## Verification

Add a regression test proving `confidence=0` and `confidence=1` normalize to
floating-point values. Preserve tests proving direct construction rejects
integers and that invalid/bool values remain rejected. Run the focused Vision
contract, provider, router, and delivery suites, followed by the full offline
gate required by Stage B.
