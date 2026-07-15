# Task 16-C Implementation Prompt

Use Claude Code with explicit model `glm-5.2[1m]`. Execute only Bridge
capability negotiation and read-only ChangeSet preflight. Do not start Apply,
receipt execution, rollback, workspace public commands, or Task 16-D.

## Objective

Add the fail-closed `changeset.v1` Bridge capability and one typed
`changeset.preflight` operation. Preflight must run through the accepted shared
main-thread FIFO and return bounded current scene facts needed to independently
verify a ChangeSet. It must not mutate Houdini.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-15-secure-houdini-bridge-readonly-design.md`
3. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`,
   especially sections 2, 4, 7, 8, and 9
4. `docs/superpowers/plans/2026-07-15-typed-changeset-policy.md`, Task 16-C
5. Accepted Task 16-A contracts/policy and Task 15 Bridge contracts/client/
   queue/server
6. Their focused tests

Current accepted tip is `7b3bff8`. Preserve every Codex-owned uncommitted doc;
do not edit, stage, restore, or commit `docs/`.

## Authorized files

- Create `eee_agent/houdini_bridge/changesets.py`
- Modify `eee_agent/houdini_bridge/client.py`
- Modify `eee_agent/houdini_bridge/__init__.py`
- Modify `houdini_side/secure_bridge.py` only for capability advertisement and
  strict typed dispatch
- Create `houdini_side/changeset_executor.py` only for read-only preflight
  facts if separation from the existing adapter is cleaner
- Modify `eee_agent/houdini_bridge/queue.py` only if a name-neutral alias is
  necessary; do not change accepted FIFO/cancellation semantics
- Create `tests/runtime/test_changeset_bridge_contracts.py`
- Create `tests/runtime/test_changeset_bridge_preflight.py`
- Extend focused client/queue/transport tests

No Runtime database/service/protocol, ChangeSet contracts/policy/repository,
agent, UI, dependency, lock, or documentation file is authorized.

## Required capability negotiation

- Keep protocol `eee.bridge/1` and the same token hello request.
- Successful hello ack adds exact, sorted, unique `capabilities`; Task 16-C
  server advertises `changeset.v1`.
- New clients accept legacy successful hello acks without `capabilities` for
  existing `scene.query`, treating capabilities as empty.
- Malformed/non-list/duplicate/unsorted/non-string capability acks fail closed.
- `BridgeClient.preflight()` must check `changeset.v1` before sending any
  request. Absence raises `bridge.capability_unavailable` and sends no frame.
- Existing read-only `scene.query` wire behavior remains unchanged.

## Strict preflight contracts

Define frozen/slotted, exact-field DTOs in
`eee_agent/houdini_bridge/changesets.py`. Reuse Task 16-A `ChangeSet`,
`WorkspaceManifest`, `NodeRef`, `WireRef`, typed conditions and canonical JSON;
do not create permissive operation dictionaries.

The preflight request must carry exactly:

- the full canonical schema-v1 `ChangeSet` and its exact SHA-256 digest;
- the matching `WorkspaceManifest` when `workspace_id` is present, otherwise
  `null`;
- the generic request ID/deadline/scene epoch envelope facts.

Construction and parsing must verify ChangeSet digest, manifest identity/
revision, session/workspace/binding consistency, exact operation allowlist,
strict JSON, size limit, duplicate keys, and no unknown fields.

The response must contain only bounded typed facts:

- current `SceneBinding`;
- verified workspace ID/revision or `null`;
- one deterministic node fact per affected/read/operation reference, including
  requested NodeRef, existence, actual path/type, mirrored workspace/node ID,
  capability/role, and locked state;
- parameter facts for every referenced parameter, including existence and a
  bounded supported scalar/tuple value;
- wire facts for every referenced input, including the actual typed source or
  `null`;
- condition results for every supplied precondition; and
- an overall `all_preconditions_hold` boolean derived from the results.

Facts are deterministically ordered and deep immutable. Missing/ambiguous
identity, duplicate stable IDs/paths, unsupported parameter values, oversized
facts, unknown operations, stale binding/epoch/revision, or ownership mismatch
must return bounded structured errors and `scene_may_have_changed=false`.

## Houdini-side preflight

- Parse operation names through explicit typed dispatch: only `scene.query`
  and `changeset.preflight` exist in this slice.
- Queue admission validates token/hello, capability, size, deadline, scene
  epoch, digest, and request shape before the main-thread callable runs.
- Reads and preflight share the one existing bounded FIFO; no second queue,
  worker, task, or concurrent HOM path.
- Resolve stable owned identity using only mirrored keys:
  `eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
  `eee.schema_version`, and `eee.created_by_run`; path alone is never identity.
- Detect ID/path ambiguity and fail closed. Verify type, parent, ownership,
  lock, parameter and wire facts on the main thread.
- Never call create/destroy/set/setInput/setUserData, undo, save/load/clear,
  file, HDA, shell, Python/VEX installation, or any other mutation method.
- Do not trust Runtime's `PolicyDecision`; preflight derives facts from the
  current fake/real scene and the immutable request.

## Test-first requirements

Begin with genuine RED tests. Include:

- legacy/new hello compatibility, exact sorted capabilities, malformed ack,
  and absent capability sends no preflight frame;
- strict request/response round-trip, immutability, unknown fields/tags,
  digest/manifest/binding mismatch, duplicate keys and size boundaries;
- wrong token, stale epoch, stale manifest revision, missing node, ID/path/type/
  workspace mismatch, duplicated/ambiguous identity, locked node;
- exact parm and wire facts, unsupported/unbounded parm values;
- FIFO ordering with concurrent `scene.query` and preflight, deadline,
  queued/running cancellation and shutdown behavior;
- a mutation-spy fake scene proving preflight performs zero writes;
- existing client/queue/transport and Task 16 contract/policy regressions.

## Verification

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py -q
uv run --extra eval pytest tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
```

Only the existing optional WSL probe may skip.

## Commit and handoff

Stage only authorized files and commit once:

```text
feat: preflight typed houdini changesets
```

Return commit/parent, exact file list, RED evidence, focused/full counts,
capability compatibility, FIFO/no-write evidence, and concerns. Do not push,
merge, amend, clean docs, start B2b, or start Apply/Task 16-D.
