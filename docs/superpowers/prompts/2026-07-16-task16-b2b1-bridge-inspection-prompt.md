# Task 16-B2b-1 Implementation Prompt

Use this prompt either for direct Codex implementation or, only when the user
explicitly selects it, an optional external coding worker. Execute only the
strict `workspace.v1` Bridge inspection foundation. Do not modify Runtime
persistence, activate public workspace commands, or start B2b-2/B2b-3/Task
16-E.

## Objective

Add one authenticated, capability-gated, read-only `workspace.inspect`
operation. It must inspect either the exact current selection or a supplied
trusted `WorkspaceManifest`, gather bounded live identity facts on Houdini's
main thread, detect duplicate EEE identity, and perform zero scene writes.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
3. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`,
   through Tasks 1-3
4. Accepted Bridge contracts/client/queue/server and ChangeSet Bridge tests
5. `eee_agent/changesets/contracts.py` `OwnedNodeRef` and
   `WorkspaceManifest`

Current accepted docs tip is `e135088`. Preserve every Codex-owned uncommitted
document. Do not edit, stage, restore, clean, or commit `docs/`.

## Authorized files

- Create `eee_agent/houdini_bridge/workspaces.py`
- Create `eee_agent/houdini_bridge/workspace_provider.py`
- Modify `eee_agent/houdini_bridge/client.py`
- Modify `eee_agent/houdini_bridge/__init__.py`
- Create `houdini_side/workspace_inspector.py`
- Modify `houdini_side/secure_bridge.py`
- Create `tests/runtime/test_workspace_bridge_contracts.py`
- Create `tests/runtime/test_workspace_bridge_inspector.py`
- Extend `tests/runtime/test_houdini_bridge_client.py`
- Extend `tests/runtime/test_houdini_bridge_queue.py`
- Extend `tests/runtime/test_houdini_bridge_transport.py`

No Runtime database/protocol/server/service/CLI, ChangeSet repository/service,
executor, agent, UI, dependency, lock, or documentation file is authorized.

## Exact contracts

- Capability: `workspace.v1`.
- Operation: `workspace.inspect`.
- Request envelope fields are exactly `protocol`, `kind`, `request_id`,
  `operation`, `deadline_ms`, `scene_epoch`, and `payload`.
- Payload fields are exactly `mode` and `manifest`.
- `mode="selection"` requires manifest `null`; `mode="manifest"` requires an
  exact schema-v1 `WorkspaceManifest`.
- `scene_epoch` is exact `int >= 1` or null and is an optimistic read check,
  never write authority.
- Result contains exact `SceneBinding`, mode, deterministic immutable
  observations, canonical observed SHA-256, and
  `scene_may_have_changed=false`.
- Each observation contains path, node type, parent path, lock state, and
  nullable exact values for all six mirrors: workspace ID, node ID,
  capability, role, schema version, and creating Run.
- Exact JSON, duplicate-key rejection, unknown-field rejection, finite bounds,
  strict primitive types, and the accepted 1 MiB limit apply at every parser.
- Old Bridge processes without `workspace.v1` fail before the client sends an
  inspect frame. Existing `scene.query` and every `changeset.v1` operation stay
  wire-compatible.

## Houdini inspection rules

- Selection mode returns exactly selected nodes. It adds no descendants,
  inputs, outputs, siblings, or neighbors.
- Manifest mode ignores current selection and resolves every manifest node by
  stable EEE node ID; stored path is corroboration only.
- Both modes perform a bounded scan sufficient to detect a copied duplicate of
  every in-scope stable ID. Duplicate IDs/paths are conflicts.
- Only bounded path/type/parent, hard/soft lock, and six user-data values may
  be read. No raw HOM object or arbitrary user-data map crosses the boundary.
- All HOM access runs inside the accepted `MainThreadReadQueue`. Reads,
  workspace inspection, ChangeSet preflight, Apply, and receipt share that one
  FIFO.
- Never call create/destroy/set/setInput/setUserData, flags, undo, save/load/
  clear, HDA/file/export/shell/code operations, or generic method dispatch.

## Production provider

Add `BridgeWorkspaceFactProvider` over `paths.state_dir`. Each call uses
`BridgeClient.from_state_dir`, opens, sends one typed request, and closes, so a
Houdini restart or new port is rediscovered. Normalize ordinary missing/closed/
timeout/capability absence to `WorkspaceInspectionUnavailable` and exact live
identity ambiguity to `WorkspaceInspectionConflict`. Do not hide malformed
authentication, malformed protocol, or a corrupt result as ordinary offline or
identity conflict state.

## Test-first requirements

Begin with genuine RED tests. Cover strict round trips, immutability, canonical
ordering/hash, invalid mode/manifest pairs, partial mirrors, empty selection,
exact selection-only behavior, rename/move, locks, missing nodes, duplicate
identity, stale epoch, scan/size bounds, token auth, old-server no-send,
malformed capabilities, request-ID mismatch, structured errors, mixed-operation
FIFO/capacity/deadline/cancellation/shutdown, and mutation-spy fingerprints.

## Verification

```powershell
uv run --extra eval pytest tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py -q
uv run --extra eval pytest tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_bridge_transport.py -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
```

No new skip or xfail is allowed.

## Commit and handoff

Stage only authorized implementation/test files and commit once:

```text
feat: inspect trusted houdini workspaces
```

Return commit/parent, exact changed files, initial RED evidence, focused counts,
capability/no-send evidence, single-FIFO evidence, mutation-spy evidence, and
concerns. Do not push, merge, amend, clean docs, or begin B2b-2.
