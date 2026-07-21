# Task 18-C Runtime Modeling Proposal Integration Design

- Date: 2026-07-16
- Scope: Optional Runtime graph injection and verified minimal Houdini catalog
- Status: Approved for local implementation

## Goal

Make the accepted Task 18-B `propose_modeling` seam available to the
production Runtime Agent only when explicitly constructed with
`modeling=True`. The existing default AgentRunner and exact seven-tool
read-only tests remain unchanged.

## Production catalog

The catalog is developer-owned and contains only facts verified against
Houdini 21.0.440.440 with local hython:

- `box`: `sizex/sizey/sizez` default `1.0`, `tx/ty/tz` default `0.0`,
  `rx/ry/rz` default `0.0`, one input;
- `xform`: `tx/ty/tz` default `0.0`, `rx/ry/rz` default `0.0`,
  `sx/sy/sz` default `1.0`, one input;
- `grid`: `sizex/sizey` default `10.0`, `rows/cols` default `10`, zero inputs;
- `null`: `cacheinput` default `0`, `copyinput` default `1`, one input; and
- `merge`: no parameters, bounded to 64 inputs.

No VEX/Python/source/file/expression parameter is included. `transform` is
explicitly not included because Houdini reported it as an invalid type name;
the verified SOP type is `xform`.

## Runtime context injection

`build_agent_runner(checkpointer, modeling=False)` preserves the current
read-only graph by default. With `modeling=True`, it adds only
`propose_modeling` and the `ModelingToolContext` schema.

`RuntimeService` installs a context factory on an AgentRunner that supports it.
At the beginning of each Run the factory:

1. obtains the current SceneBinding from the trusted ChangeSet Bridge seam;
2. inspects the active Workspace through the existing WorkspaceService;
3. requires a healthy exact manifest/scene binding;
4. creates a `ModelingProposalCoordinator` with the verified catalog; and
5. binds `RuntimeService.propose_changeset_trusted` as the one proposal
   callback.

If catalog, Workspace, Bridge binding, or health is unavailable, the context is
`None`; the tool fails closed without failing a read-only Run.

The context is frozen for the Run. A SceneBinding/Workspace change affects the
next Run only; Apply remains outside the model tool.

## CLI wiring

The production `runtime serve` path constructs
`build_agent_runner(..., modeling=True)` and supplies the verified catalog
provider. Test fixtures and direct callers continue to use their existing
one-argument runner factories and read-only defaults.

## Acceptance

- production catalog defaults and node bounds match the hython evidence;
- default AgentRunner remains exactly read-only;
- modeling AgentRunner adds only `propose_modeling`;
- context factory fails closed on unavailable/stale Workspace/Bridge;
- context is frozen per Run and callback proposals persist through the trusted
  service seam only;
- no public WebSocket command or Apply path is added;
- full offline suite, lock, compileall, and diff checks; and
- manual Houdini gate: create/bind Workspace, run an explicit modeling request,
  confirm one AwaitingApproval ChangeSet summary, approve/reject exact digest,
  verify no Apply occurs before the next explicit trusted integration, and
  confirm zero scene mutation before approval.
