# Task 17-B Interactive Runtime Panel Design

## 1. Scope

Task 17-B extends the accepted docked Runtime panel from a read-only scene
observer into a bounded interactive control plane for:

- Session creation and selection;
- starting and stopping one Runtime Run;
- replay/snapshot-based Run status and output recovery;
- durable ChangeSet approval queue recovery;
- bounded risk preview;
- explicit approve or reject decisions;
- terminal receipt and critical-recovery rendering.

It does not add a public `changeset.apply` command, accept operation JSON from
the panel, expose parameter values, start Runtime, own SQLite, import the agent
graph into Houdini, call HOM directly, or bypass the typed
Workspace/ChangeSet/approval/preflight/Apply/receipt/recovery pipeline.

Task 18 remains responsible for compiling model output into a trusted typed
ChangeSet and invoking the existing in-process proposal/Apply methods. Task
17-B renders and decides that trusted state without receiving raw write
authority.

## 2. Product job and visual direction

The subject is a Houdini technical director supervising one live Runtime
Session. The panel's single job is to make the current control boundary
obvious: what is running, what is merely proposed, and what the user is
actually authorizing.

The accepted control-plane palette remains:

- Graphite `#17191D`
- Slate `#20242A`
- Iron `#343A43`
- Signal cyan `#63C7C9`
- Gate amber `#FF7A1A`
- Fault red `#E45B55`
- Text `#E7E9EC`
- Dim text `#9097A1`

Typography remains deliberately instrument-like:

- `Bahnschrift SemiCondensed` for the panel title and Run state;
- `Segoe UI` for readable prompts and summaries;
- `Consolas` for identifiers, epochs, digests, statuses, and counters.

The layout is one narrow dock with three work surfaces:

```text
+--------------------------------------------------+
| EEE / CONTROL PLANE       ONLINE   BRIDGE READY |
| HIP / Session / seq                              |
| INSTANCE          EPOCH          REVISION        |
+--------------------------------------------------+
| RUN | APPROVALS | SCENE                          |
+--------------------------------------------------+
| active surface                                   |
|                                                  |
|                                                  |
+--------------------------------------------------+
```

The signature element is the **approval gate ticket**. It is not a generic
dashboard card. Its structure encodes the actual boundary:

```text
GATE / AWAITING APPROVAL              expires 03:42
ProjectChange · 14 operations · wiring · backup
--------------------------------------------------
/obj/house/walls
/obj/house/windows
+ 9 more paths
--------------------------------------------------
[Reject]                            [Approve bound]
```

The amber gate strip is reserved for a pending authorization. Cyan represents
read-only/ready state, and red is reserved for failed or critical-recovery
state. No decorative gradients, metric tiles, or chat bubbles are introduced.

## 3. Runtime protocol additions

### 3.1 Existing commands used by the panel

The panel may use only these accepted public commands:

- `runtime.ping`
- `session.list`
- `session.create`
- `session.subscribe`
- `session.snapshot`
- `run.start`
- `run.stop`
- `run.force_stop`
- `changeset.approve`
- `changeset.reject`

The client builder validates an exact payload schema for every permitted
command. It does not become a general Runtime command serializer.

### 3.2 New read-only command: `changeset.list`

Request:

```json
{
  "session_id": "ses_<uuid>",
  "limit": 50
}
```

Rules:

- exact Session ID;
- exact integer limit in `1..50`;
- newest records first;
- scoped to that Session;
- one consistent repository read;
- response remains comfortably below the Runtime message limit.

Response:

```json
{
  "changesets": [
    {
      "change_id": "chg_<uuid>",
      "run_id": "run_<uuid>",
      "state": "AwaitingApproval",
      "changeset_digest": "<64 lowercase hex>",
      "created_at": "<UTC ISO-8601>",
      "required_permission": "ProjectChange",
      "risk": {
        "operation_count": 14,
        "touches_external_nodes": false,
        "changes_wiring": true,
        "requires_backup": true,
        "effect_count": 3,
        "effect_names": ["node.create", "parm.set", "wire.connect"],
        "affected_path_count": 11,
        "affected_paths": ["/obj/house/walls", "/obj/house/windows"],
        "affected_paths_truncated": true
      },
      "approval": {
        "approval_id": "apr_<uuid>",
        "decision": "Pending",
        "expires_at": "<UTC ISO-8601>",
        "decided_at": null,
        "approved_instance_id": null,
        "approved_scene_epoch": null
      },
      "receipt": null
    }
  ]
}
```

Bounded summary rules:

- at most 12 effect names;
- at most 12 affected paths;
- full counts accompany truncated lists;
- no operation DTOs;
- no parameter values;
- no checkpoint or pre/postcondition DTOs;
- no bearer token, raw Bridge error, traceback, or HOM value;
- receipts expose only status, revisions, scene identity, applied operation
  count, completion time, and `scene_may_have_changed`.

The command is read-only and remains available during a write freeze.

## 4. Recovery and state model

After choosing a Session, the panel:

1. subscribes from its per-Session cursor;
2. requests `session.snapshot`;
3. requests `changeset.list`;
4. applies live persisted events in sequence.

The Run surface uses the snapshot's newest 100 Runs and exact `active_run`.
Live events update the active Run, status, bounded output, tools, usage, final
response, and structured failure.

The approval surface treats `changeset.list` as durable authority. Relevant
live events trigger a refresh rather than attempting to reconstruct full
approval state from event history:

- `changeset.proposed`
- `approval.requested`
- `approval.approved`
- `approval.rejected`
- `approval.expired`
- `changeset.state_changed`
- `changeset.applied`
- `changeset.rolled_back`
- `recovery.critical`

This keeps reconnect, retention-gap, Runtime restart, and decision-race
behavior coherent.

## 5. Interaction rules

### 5.1 Session and Run

- No active Session: the Run surface offers `New Session`.
- Session creation requires a non-empty title and then refreshes/selects it.
- `Start run` requires an online Runtime, selected active Session, non-empty
  prompt, and no active global Run.
- Closing the panel never stops a Run.
- `Stop run` sends cooperative `run.stop`.
- `Force stop` is shown only after StopRequested/Stopping or explicit user
  confirmation.
- A disconnect never implies cancellation; the panel reconnects and recovers
  from snapshot/events.

### 5.2 Approval gate

- Approve and Reject are enabled only for `AwaitingApproval` plus `Pending`.
- The panel sends the exact displayed `change_id` and canonical digest.
- Approve never sends scene identity; Runtime binds the decision to the
  current Bridge scene.
- The UI labels approval as authorization of the displayed bounded risk, not
  as immediate Apply.
- After any decision response or related event, `changeset.list` refreshes.
- Expired/stale/consumed/conflicting decisions render the structured Runtime
  error and disable repeat actions.
- Applied/RolledBack/CriticalRecovery tickets are read-only evidence.

There is no Apply button.

## 6. Client security boundary

The Qt client:

- reads only Runtime discovery/token handoff files;
- keeps the token repr-hidden and out of UI/log state;
- uses one authenticated QWebSocket;
- validates compatible response/event envelopes;
- tracks request purpose by request ID;
- exposes no arbitrary `send(command, payload)` public UI hook;
- never opens Runtime databases;
- never imports the agent graph, ChangeSet executor, Bridge writer, rpyc, or
  HOM.

The Secure Bridge remains Houdini-owned and all Houdini operations remain on
the accepted single main-thread FIFO.

## 7. Failure states

The UI renders bounded, actionable states:

- Runtime offline: start Runtime and retry automatically.
- No Session: create one.
- Active Run elsewhere: continue observing it or request Stop.
- Approval expired: refresh; the ticket becomes read-only.
- Scene stale/binding unavailable: decision fails without state mutation.
- Critical recovery: red evidence ticket; further writes remain frozen.
- Protocol/auth/internal failure: bounded message, no traceback or credential.

## 8. Acceptance

Offline acceptance proves:

- exact `changeset.list` request/response bounds and Session scoping;
- storage corruption fails closed;
- no raw operations/parameter values leak through summaries;
- client command allowlist and payload schemas are exact;
- snapshots and live events recover Run state without cursor regression;
- relevant ChangeSet events cause authoritative refresh;
- duplicate decision clicks cannot send concurrent commands;
- the panel source contains no SQLite, graph, HOM, legacy rpyc, Apply command,
  or unrestricted serializer;
- full Runtime regression remains green.

Real Houdini acceptance proves:

- create/select a Session;
- start a read-only Run and observe status/output;
- close/reopen and restart Runtime without losing the active/terminal Run;
- Stop does not block the Houdini UI;
- the approval surface has a correct empty state until Task 18 proposes a
  ChangeSet;
- scene and filesystem mutation remain absent for read-only Runs.

Actual pending-approval decision smoke is required when Task 18 first produces
a trusted proposal; it is not simulated through an unsafe panel backdoor.
