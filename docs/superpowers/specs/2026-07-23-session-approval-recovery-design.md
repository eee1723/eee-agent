# Session creation and ChangeSet approval/recovery UX

## Goal

Make the Runtime panel create conversations without a title prompt, keep
empty conversations idempotent, and make the ChangeSet approval result
truthful when approval commits but the trusted Apply boundary is blocked by a
durable recovery condition.

## Session behavior

`New session` is an internal placeholder only. The panel renders it as
`未命名对话`; users never enter a title during creation. The existing
best-effort title model remains responsible for renaming the placeholder
after the first completed Run.

Repeated creation requests must not create duplicate empty conversations.
The Runtime repository treats an active placeholder with no Run as the
canonical empty conversation and returns it idempotently. The panel also
coalesces an in-flight create request so rapid clicks do not issue parallel
requests.

## ChangeSet behavior

Approval remains durable before Apply. The Runtime keeps the existing
fail-closed `CriticalRecovery` write barrier. When approval commits but Apply
is blocked by that barrier, the command response contains both facts:

* the approval decision is `Approved`;
* the Apply result is `BlockedRecovery`, with the blocking ChangeSet IDs.

The panel renders this as an approval-success/recovery-required notice. It
does not delete, acknowledge, or bypass a critical recovery record.

## Non-goals

* No automatic database cleanup or destructive recovery.
* No weakening of the typed Bridge write freeze.
* No change to the title-generation prompt or provider selection.
* No new unrestricted Apply or recovery route.

## Verification

Regression coverage will assert:

* empty placeholder creation is idempotent while a session has no Run;
* non-empty titled sessions and placeholder sessions with a Run still create
  independently;
* the panel no longer requires `SessionTitleDialog`;
* an approval blocked by `recovery.critical_required` returns a structured
  approved/blocked result and the panel uses recovery wording.
