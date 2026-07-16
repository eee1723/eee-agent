# Task 19 Artifacts, Vision, and Evaluation Plan

## 19-A Artifact store and capture

- immutable content-addressed metadata;
- bounded retention and redaction;
- deterministic camera/framing preflight;
- exact-byte/hash parity between viewer and vision input;
- capture unavailable/failed states.

## 19-B Vision routing

- explicit provider capability resolution;
- schema-validated normalized visual report;
- user-visible skip/waiver when unavailable;
- no visual override of deterministic failures;
- raw response stored only as redacted artifact.

## 19-C Evaluation and delivery

- Golden Case scoring and regression budgets;
- final DecisionSummary, ValidationReport, ChangeReceipt, artifacts, and
  parameter guide;
- optional Phoenix/LangSmith export outside correctness path;
- restart/replay and retention tests.

Hython can cover geometry/camera/capture inputs. Final viewport appearance and
viewer interaction remain real Houdini GUI acceptance.

