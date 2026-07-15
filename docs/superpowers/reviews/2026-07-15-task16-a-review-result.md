# Task 16-A Independent Review Result

- Reviewed commit chain: `6b94cb5` -> `9f750ae` -> `6b860ff` -> `dff573f`
- Baseline parent: `5915720`
- Review date: 2026-07-15 (Asia/Shanghai)
- Result: **Accepted at `79f281d`**
- Promotion: Task 16-B may receive its separate implementation prompt; it has
  not started

## Positive verification

- The commit contains exactly the six reported authorized implementation/test
  files. Codex-owned documentation remains outside the commit.
- `uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q` passed: 138.
- Focused regression passed: 374.
- Independent full offline suite passed: 1525 passed, 1 pre-existing optional
  WSL probe skipped.
- `uv lock --check`, compileall, and commit diff check passed.
- Static import review found no `hou`, `rpyc`, legacy bridge, transport,
  database, subprocess, or filesystem-write import in the new production
  package.
- The reported `tests/core/test_ids.py` path is the actual repository path and
  is the correct regression target.
- The follow-up added the requested F1-F4 coverage and those original
  reproductions now fail closed.
- The second follow-up added F5/F6 coverage; all six previously reported
  external-touch and affected-fact reproductions now fail closed.
- The third follow-up closes the duplicate-create and OwnedWorkspace manifest
  reuse portions of F7. Its no-manifest ProjectChange external-risk behavior is
  accepted as the intended conservative fail-closed policy.

## Blocking findings

### F1 — Affected/read references are not unique by stable node identity

Location: `eee_agent/changesets/contracts.py:185-191,1055-1060`

`_unique_canonical()` compares the complete serialized `NodeRef`. Two entries
with the same stable `node_id` but different path/type/workspace facts are
therefore accepted. The design treats `node_id` as identity and the prompt's
review cases explicitly require duplicate node ID at a different path to fail.

Independent reproduction:

```text
affected_nodes=(
  NodeRef("n_child", "/obj/ws/c", "geo", ws),
  NodeRef("n_child", "/obj/ws/renamed", "geo", ws),
)
```

Result: `ChangeSet` constructs successfully and policy returns `allowed=True`.

Required fix:

- Reject duplicate identity keys in both `affected_nodes` and
  `read_dependencies`; identity is `node_id` when present, otherwise path.
- Reject contradictory duplicate paths even when IDs differ.
- Add regression tests for same-ID/different-path, same-path/different-ID,
  and the corresponding read-dependency cases.

### F2 — OwnedWorkspace does not validate the complete manifest identity

Location: `eee_agent/changesets/policy.py:155-172`

Owned policy checks only whether a changed `node_id` is present in the
manifest. It does not compare the target/source `path`, `expected_type`, or
`expected_workspace_id` against the manifest's owned fact. A target can claim
the right owned ID while pointing at a renamed path, a wrong type, or another
workspace and still be allowed.

Independent reproductions:

```text
NodeRef("n_child", "/obj/ws/renamed", "geo", ws)      -> allowed=True
NodeRef("n_child", "/obj/ws/geo1", "geo", ws_other)  -> allowed=True
```

Required fix:

- Build manifest indexes by stable ID and path.
- For every changed target and every owned wire source, require exact path,
  expected type, and expected workspace match to the manifest fact.
- Preserve external read-only parent/source behavior only where the design
  explicitly permits it; never infer ownership from ID alone.
- Add regressions for target and source path/type/workspace mismatches.

### F3 — OwnedWorkspace and ScopedPatch do not require changed targets in affected_nodes

Location: `eee_agent/changesets/policy.py:96-124,175-191`

Only `ProjectChange` checks that operation targets are enumerated by
`affected_nodes`. An OwnedWorkspace or ScopedPatch ChangeSet whose `parm.set`,
`wire.connect`, or created node is omitted from `affected_nodes` is still
allowed. This violates the explicit affected-target omission rule and makes the
user-facing preview/risk summary incomplete.

Independent reproduction:

```text
ChangeSet(operations=(SetParm(target),), affected_nodes=())
```

Result: OwnedWorkspace policy returns `allowed=True`.

Required fix:

- Derive all changed target identities (including created node IDs and both
  wire endpoints as required by the approved scope rules).
- Require each derived target/path to be present in `affected_nodes` for every
  permission mode; return stable `policy.affected_target_omitted` otherwise.
- Keep ProjectChange's existing enumeration check and add cross-mode tests.

### F4 — Risk summary can under-report external effects and affected paths

Location: `eee_agent/changesets/policy.py:84-90`

Policy validates only `effect_names`, `changes_wiring`, and `operation_count`.
It ignores `RiskSummary.touches_external_nodes` and `affected_paths`. A
`wire.connect` from an external node can declare `touches_external_nodes=False`
and omit the source/target path while policy returns `allowed=True`.

Independent reproduction: an external source in `read_dependencies` with
`touches_external_nodes=False` returns an allowed decision.

Required fix:

- Derive affected paths and whether any operation touches an external node from
  the typed operations plus manifest/read facts.
- Reject under-reporting and contradictory over-reporting with
  `policy.effect_contradiction` (or a separately documented stable code).
- Add tests for external source/parent, missing target/source paths, and both
  true/false risk flags.

## Historical blockers after 9f750ae (fixed by 6b860ff)

### F5 — External-touch fallback is still permissive for ProjectChange

Location: `eee_agent/changesets/policy.py:160-210`

The new external-touch derivation does not include `changed_targets` at all,
so an external `ProjectChange` `parm.set` target can declare
`touches_external_nodes=False` and remain allowed. More importantly, when no
manifest exists and `changeset.workspace_id is None`, `_is_external_reference`
returns `False` when `expected_workspace_id is None`; an external wire source
with no declared workspace therefore passes with an under-reported risk flag.
With a manifest, classification is by `node_id`/path membership alone, so a
source that reuses an owned ID while pointing at an external path is also
treated as internal in ProjectChange.

Independent reproductions on `9f750ae`:

```text
ProjectChange(SetParm(external_target), workspace=None,
              touches_external_nodes=False) -> allowed=True
ProjectChange(WireConnect(external_source), workspace=None,
              touches_external_nodes=False) -> allowed=True
ProjectChange(WireConnect(source=node_id=owned_id, path=external_path),
              workspace=manifest, touches_external_nodes=False) -> allowed=True
```

Required fix:

- Include changed targets in external-touch derivation.
- Without a manifest, fail closed for ownership evidence: references whose
  ownership cannot be proven must be treated as external for risk reporting.
- With a manifest, require exact ID/path/type/workspace fact matching before a
  reference is considered internal; an ID-only match is insufficient.
- Add ProjectChange wire-source and SetParm target tests for both no-manifest
  and manifest-present cases.

### F6 — affected_nodes can enumerate the right ID with contradictory facts

Location: `eee_agent/changesets/policy.py:141-150`

F3 currently matches operation targets to `affected_nodes` only by stable
identity. A ChangeSet can therefore use the real operation target
`NodeRef(node_id="n_child", path="/obj/ws/geo1", ...)` but enumerate
`affected_nodes=(NodeRef(node_id="n_child", path="/obj/external", ...),)` and
still be allowed. The user-facing affected-node fact is then contradictory to
the operation and can mislead approval/review; the same issue applies to wire
sources and created-node facts.

Independent reproduction on `9f750ae`: `OwnedWorkspace` policy returns
`allowed=True` for the above mismatched affected-node fact.

Required fix:

- For every derived operation target/source/create ref, require the matching
  affected node to agree on the complete bounded facts (identity, path,
  expected type, and expected workspace where applicable), not just node ID.
- Add regressions for wrong path/type/workspace in `affected_nodes` for parm,
  wire source/target, and create operations across the relevant modes.

## Historical blocker after 6b860ff (partially fixed by dff573f)

### F7 — Duplicate CreateNode IDs and manifest ID reuse remain allowed

Location: `eee_agent/changesets/contracts.py` operation validation and
`eee_agent/changesets/policy.py:161-181,235-278`

`ChangeSet` enforces unique `op_id` values but not unique created `node_id`
values or derived create paths. Two identical `node.create` operations with
different operation IDs can therefore pass the affected-node and risk checks
and return `allowed=True`. In OwnedWorkspace, a create can also reuse an
already-owned manifest node ID at a new path and return `allowed=True`.

Independent reproductions on `6b860ff`:

```text
CreateNode(c1, node_id="n_new", parent=/obj/ws, name="x")
CreateNode(c2, node_id="n_new", parent=/obj/ws, name="x")
  -> OwnedWorkspace allowed=True

CreateNode(node_id="n_child", parent=/obj/ws, name="new")
  -> OwnedWorkspace allowed=True when n_child already exists in manifest
```

Required fix:

- Reject duplicate created node IDs and duplicate derived create paths within a
  ChangeSet, preferably in the immutable contract constructor with dedicated
  regression tests. Different operation IDs must not make the create effect
  repeatable.
- In OwnedWorkspace, reject a CreateNode whose node ID already exists in the
  current manifest (stable IDs are not reusable). Use a stable policy denial
  code already defined by the design or add one explicitly.
- Add positive coverage for distinct new IDs and distinct derived paths.
- Ensure created IDs cannot be used to make a conflicting source reference look
  internal during external-touch derivation.

## Historical blocker after dff573f (fixed by 79f281d)

### F8 — ProjectChange can reuse a manifest-owned stable node ID

Location: `eee_agent/changesets/policy.py:235-283`

`policy.node_id_reused` is currently added only inside `_evaluate_owned`.
`ProjectChange` skips that mode-specific function, so the same CreateNode that
is correctly denied in OwnedWorkspace becomes allowed when the permission mode
is changed to ProjectChange, even when the exact current manifest is supplied.
Stable `eee.node_id` uniqueness is an identity invariant, not a permission-mode
privilege.

Independent reproduction on `dff573f`:

```text
ProjectChange(
  CreateNode(node_id="n_child", parent=/obj/ws, name="new",
             workspace_id=current_workspace),
  workspace=current_manifest_containing_n_child,
) -> allowed=True
```

Required fix:

- When a manifest is supplied, reject every CreateNode whose node ID already
  exists in that manifest before permission-mode dispatch, using the existing
  stable `policy.node_id_reused` code.
- Add a ProjectChange-with-manifest regression for reused ID and a positive
  distinct-ID case. ScopedPatch remains independently denied from all create
  operations.
- Do not weaken the accepted no-manifest fail-closed external-risk behavior;
  absence checking without a manifest remains a later Bridge preflight concern.

## Acceptance gate for the follow-up

The final follow-up modified only the Task 16-A authorized production/test
files, added the F8 regression and positive case, and preserved all existing
tests. Codex independently ran:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_houdini_bridge_contracts.py tests/runtime/test_models.py tests/core/test_ids.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
uv lock --check
git diff --check
```

## Final acceptance record

At `79f281d`, Codex independently verified:

- ProjectChange plus a supplied manifest rejects a reused stable node ID with
  `policy.node_id_reused`;
- the corresponding distinct new ID is allowed;
- all F1-F8 adversarial reproduction groups fail closed as designed;
- Task 16-A focused suite: 187 passed;
- focused regression: 423 passed;
- complete offline suite: 1574 passed, with only the pre-existing optional WSL
  environment probe skipped;
- compileall, `uv lock --check` (69 packages), commit diff check, import
  boundary, and authorized-file scope all passed.

Task 16-A is complete and Codex-accepted at `79f281d` (implementation chain
`6b94cb5`, `9f750ae`, `6b860ff`, `dff573f`, `79f281d`). Task 16-B is unblocked
for a new, separate prompt and review gate; no Task 16-B implementation is part
of this acceptance.
