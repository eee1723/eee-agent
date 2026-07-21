# Task 18 Strict Modeling Foundation Design

- Date: 2026-07-16
- Scope: Task 18-A through Task 18-D boundaries
- First implementation slice: Task 18-A contracts and deterministic compiler
- Status: Approved for local implementation

## 1. Goal

Task 18 turns model-authored modeling intent into the existing trusted typed
ChangeSet pipeline without giving the model ChangeSet, Bridge, HOM, Python,
VEX, shell, filesystem, or Apply authority.

The first slice establishes the pure-Python foundation:

1. strict, versioned ModelingBrief, ProceduralSpec, ComponentSpec,
   QualityProfile, RepairBudget, and RepairTicket contracts;
2. a trusted node catalog that owns allowed node types, parameter defaults,
   parameter shapes, and input/output bounds;
3. a deterministic compiler that accepts a validated ProceduralSpec plus the
   exact WorkspaceManifest and produces only an immutable Task 16 ChangeSet;
4. deterministic spec/compiler validation and bounded error codes; and
5. offline proof that model data cannot inject operations, expected-old facts,
   expressions, source code, paths outside the selected Workspace, or hidden
   graph dependencies.

This slice does not connect the compiler to the live model, Runtime proposal
service, approval decision, Apply path, Houdini, staging, reconciliation,
capture, vision, or artifact persistence.

## 2. Security boundary

Model-authored JSON may describe only:

- a bounded modeling brief;
- components and their declared dependency DAG;
- nodes selected by exact catalog type name;
- bounded literal parameter assignments; and
- bounded input connections between declared nodes.

Model-authored JSON may not contain:

- ChangeSet IDs, operation IDs, stable executor node IDs, risk summaries,
  preconditions, postconditions, checkpoint plans, expected-old values, or
  permission modes;
- arbitrary Houdini paths, expressions, callbacks, Python, VEX, HScript,
  filenames, environment values, or serialized HOM objects;
- unknown node types, unknown parameters, heterogeneous values, non-finite
  numbers, duplicate keys, duplicate identifiers, forward-hidden
  dependencies, or cyclic component/node graphs.

The trusted compiler derives every write fact. The existing Task 16 contracts,
policy engine, approval digest, preflight, executor, and recovery path remain
the final authority.

## 3. Versioned modeling contracts

All public contracts are frozen, slotted dataclasses with:

- `schema_version=1`;
- exact primitive types (`bool` is not an `int`);
- exact field sets in `from_dict`;
- strict JSON entry points with duplicate-key rejection;
- bounded strings and collections;
- finite numeric values;
- fresh mutable JSON trees from `to_dict`; and
- canonical SHA-256 digests where identity binding is required.

### 3.1 ModelingBrief

```text
ModelingBrief
- schema_version: 1
- brief_key: bounded identifier
- title: bounded text
- asset_family: bounded identifier
- goal: bounded text
- units: Millimeters | Centimeters | Meters
- up_axis: X | Y | Z
- front_axis: PositiveX | NegativeX | PositiveY | NegativeY | PositiveZ | NegativeZ
- constraints: tuple[BriefConstraint, ...]       # 0..64
```

`up_axis` and the axis portion of `front_axis` must differ. A brief digest is
the canonical SHA-256 of the complete payload.

### 3.2 ProceduralSpec and ComponentSpec

```text
ProceduralSpec
- schema_version: 1
- spec_key: bounded identifier
- brief_digest: sha256
- quality_profile_id: bounded identifier
- workspace_root_node_id: bounded identifier
- components: tuple[ComponentSpec, ...]          # 1..64

ComponentSpec
- component_id: bounded identifier
- role: bounded identifier
- depends_on: tuple[component_id, ...]           # explicit DAG
- nodes: tuple[NodeSpec, ...]                    # 1..64

NodeSpec
- node_key: bounded identifier
- node_type: bounded catalog key
- node_name: bounded Houdini-safe identifier
- parent_node: qualified node key | null         # null = Workspace root
- parameters: tuple[ParmAssignment, ...]
- inputs: tuple[InputBinding, ...]

ParmAssignment
- parm_name: bounded identifier
- value: bounded JSON scalar or homogeneous scalar tuple

InputBinding
- input_index: non-negative int
- source_node: qualified node key
- source_output_index: non-negative int
```

Qualified node keys use the exact form `component_id.node_key`. They are
logical references, never Houdini paths.

Every cross-component parent/input reference must name a component in the
consumer component's explicit transitive `depends_on` set. The compiler rejects
cycles and undeclared hidden dependencies.

### 3.3 Quality and repair contracts

`QualityProfile` freezes the deterministic validator set and bounded budgets:

- exact validator order;
- maximum compiled node count;
- maximum parameter samples;
- maximum two repair attempts per validator stage; and
- explicit `allow_vex_source`, which is `False` for Task 18-A.

`RepairBudget` records attempts by validator stage and can only move forward.
No stage may exceed the profile limit or the architecture-wide maximum of two.

`RepairTicket` carries only a validator kind, bounded failure code/message,
bounded evidence digests, failed parameter sample labels, exact replay-boundary
digest, and attempt number. It never contains raw source, arbitrary traceback,
or write authority.

## 4. Trusted node catalog

The catalog is developer-owned input to the compiler. It is not supplied by
the model.

Each catalog entry defines:

- exact node type name;
- exact allowed parameter names;
- canonical default value for every parameter;
- parameter value shape/type;
- maximum input count; and
- maximum output index.

The compiler rejects:

- a node type absent from the catalog;
- a parameter absent from the selected node definition;
- a value whose exact scalar/tuple shape differs from the catalog default;
- an input index outside the target definition;
- a source output index outside the source definition; and
- duplicate parameter or input assignments.

The catalog has a canonical digest included in `CompilationResult` so later
Run snapshots and artifacts can identify the exact compiler capability set.

Task 18-A uses injected catalogs in offline tests. A Houdini 21.0.440-verified
production catalog is a later bounded slice; no parameter defaults are guessed.

## 5. Deterministic compilation

`compile_procedural_spec()` receives:

- exact ModelingBrief and ProceduralSpec;
- exact QualityProfile;
- exact NodeCatalog;
- exact WorkspaceManifest and SceneBinding;
- trusted Session/Run/Change IDs; and
- an aware UTC creation time.

It validates:

1. the Spec binds the exact Brief digest;
2. the requested profile ID matches the supplied profile;
3. the Workspace root ID resolves to exactly one owned manifest node;
4. manifest/session/run/binding facts agree;
5. component dependencies form a DAG;
6. node parent/input dependencies form a DAG compatible with component
   dependencies;
7. every node/parameter/input is allowed by the catalog; and
8. compiled nodes and operations remain within both profile and ChangeSet
   bounds.

It deterministically derives:

- stable executor node IDs from the Spec digest and qualified node key;
- Houdini paths from the resolved Workspace root and declared node names;
- ordered CreateNode operations;
- SetParm operations whose expected-old values come only from catalog
  defaults;
- ConnectInput operations whose expected-old source is `None` because the
  target is transaction-created;
- scene/workspace/root identity and node-absence preconditions;
- exact node/parameter/wire postconditions;
- exact affected node list;
- empty read dependencies for the closed owned graph;
- exact RiskSummary and an empty CheckpointPlan for transaction-created
  targets; and
- `OwnedWorkspace` permission only.

The resulting ChangeSet is evaluated by the existing pure Task 16 policy
engine. A compiler result is returned only if policy allows the exact digest.

## 6. Error behavior

Malformed contract input raises `TypeError` or `ValueError` at the DTO boundary.
Compilation failures use a bounded `ModelingCompileError` with a namespaced
code such as:

- `modeling.brief_mismatch`;
- `modeling.profile_mismatch`;
- `modeling.workspace_mismatch`;
- `modeling.dependency_cycle`;
- `modeling.hidden_dependency`;
- `modeling.catalog_node_denied`;
- `modeling.catalog_parm_denied`;
- `modeling.catalog_value_mismatch`;
- `modeling.input_out_of_range`; and
- `modeling.policy_denied`.

Messages contain logical keys, never tokens, raw source, arbitrary model
payloads, or Houdini exceptions.

## 7. Follow-up slices

### Task 18-A: contracts and deterministic compiler

Pure Python only. No Runtime or Houdini integration.

### Task 18-B: Runtime proposal capability

Add one model-facing structured proposal tool that accepts only strict Brief
and Spec payloads, uses a production catalog and current trusted Workspace,
compiles in process, evaluates policy, and calls
`RuntimeService.propose_changeset_trusted`. The tool returns bounded proposal
metadata to the model and panel; it never returns the full ChangeSet.

### Task 18-C: approval-to-Apply orchestration and staging

After exact user approval, Runtime invokes the existing trusted Apply path.
Introduce staging identity and reconciliation records without exposing an
Apply command or button. A real pending-approval panel smoke becomes mandatory
here.

### Task 18-D: deterministic validators and repair loop

Add structure, graph, cook, geometry, parameter sensitivity, semantic, and
artifact validators. Each failed stage creates a RepairTicket; two attempts are
the hard maximum. Reconciliation reads Houdini truth and cannot be bypassed by
editing only the Spec.

Wrangle/Python SOP source remains excluded until a separate source-policy
addendum defines hashing, static checks, cooking, sampling, and bounded source
storage.

## 8. Task 18-A acceptance

- strict JSON, exact fields, duplicate-key, Unicode, size, finite-number, and
  deep-freeze tests;
- brief axis/constraint and Spec DAG/reference tests;
- QualityProfile/RepairBudget maximum-two enforcement;
- catalog node/parameter/value/input/output denial tests;
- deterministic equal-input compilation and digest tests;
- compiler-derived expected-old, conditions, risk, checkpoint, and policy
  tests;
- injection tests proving model payloads cannot select ChangeSet fields,
  paths, expressions, source, or unsupported operations;
- import-boundary tests proving no `hou`, `rpyc`, legacy bridge, Runtime
  service, database, or LangChain dependency enters the modeling contracts or
  compiler; and
- focused suite, full offline suite, `uv lock --check`, compileall, and
  `git diff --check`.
