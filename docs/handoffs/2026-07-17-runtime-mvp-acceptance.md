# Runtime MVP acceptance handoff

Date: 2026-07-20  
Scope: deterministic Runtime protocol boundaries and the opt-in provider/HFS
journey runner from Task 7 of the Runtime MVP plan.

## Acceptance status

The four gates below are intentionally independent.  An unavailable optional
system is reported as `not run`; it is never folded into an offline pass.

| Gate | Status | Evidence |
| --- | --- | --- |
| `offline-ready` | **ready** | `tests/runtime/test_runtime_mvp_e2e.py` (8 passed); existing Runtime, ChangeSet, Artifact, Knowledge and restart suites remain the source of detailed contract coverage. |
| `real-Houdini-ready` | **ready (runner-gated)** | `tests/runtime/runtime_mvp_provider_e2e.py` requires an HFS `hython` executable and a disposable `EEE_RUNTIME_HOME`; no HFS means `not_run`. |
| `real-provider-tested` | **not run** | Requires explicit opt-in, provider credentials and `EEE_RUNTIME_MVP_PROVIDER_COMMAND`; no credentials or command means `not_run`. No provider secret is printed or persisted. |
| `GUI-accepted` | **pending** | Requires the separate Houdini GUI/manual checklist in Task 9. |

## Deterministic coverage

The acceptance file exercises the user-visible orchestration seams for empty
scene bootstrap, existing Workspace proposal/rejection/expiry, unavailable or
stale Bridge Apply, cook and validation failures, repair exhaustion,
restart/no-duplicate replay, missing Artifact, and missing Knowledge. Deep
matrices remain in the focused suites (`test_changeset_service.py`,
`test_changeset_recovery.py`, `test_workspace_service.py`,
`test_artifact_store.py`, `test_knowledge_integration.py`, and
`test_runtime_e2e.py`) so this gate does not duplicate implementation details.

Run the offline gate with:

```powershell
uv run --frozen --extra eval pytest -q tests/runtime/test_runtime_mvp_e2e.py tests/runtime
```

## Provider-gated local journey

The runner is intentionally a harness, not a fake provider success:

```powershell
$env:EEE_RUN_RUNTIME_MVP_PROVIDER_E2E = "true"
$env:EEE_RUNTIME_MVP_PROVIDER_COMMAND = "python path\\to\\provider_journey.py"
python tests/runtime/runtime_mvp_provider_e2e.py
```

The adapter command must execute the real proposal → approval → Apply/receipt
→ validation/artifact → replay → cleanup journey.  The harness supplies a
disposable `EEE_RUNTIME_HOME`, passes the detected HFS path as
`EEE_RUNTIME_MVP_HFS`, captures (and discards) stdout/stderr, enforces a
bounded timeout, and prints only a small status record.  Missing opt-in, HFS,
credentials, or command returns `{"status":"not_run", ...}`.  A non-zero
adapter exit is a failed journey, not a pass.

## Remaining follow-ups

- Run the adapter once on a machine with Houdini 21.0.440 and approved provider
  credentials; record only bounded evidence (digest, receipt status, validator
  status, artifact status and replay sequence), never prompts or secrets.
- Complete GUI acceptance separately; this document must not be updated to
  `GUI-accepted` from an offline or provider-only run.
