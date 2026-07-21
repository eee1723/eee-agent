# Task 16-C Independent Review Checklist

- Dependency: Task 16-A and B1/B2a accepted; current tip `7b3bff8`
- Scope: capability negotiation and read-only typed preflight only
- Promotion: do not start Task 16-D or B2b until all blocking items pass

## Scope and boundary

- [ ] Changed files are only the authorized C files.
- [ ] No Runtime DB/service/protocol, ChangeSet policy/repository, UI, agent,
      dependency, or docs file is in the implementation commit.
- [ ] No Apply, receipt, rollback, write-freeze, or Houdini mutation path was
      added.

## Capability and protocol

- [ ] Hello remains `eee.bridge/1` with the existing token semantics.
- [ ] New successful ack advertises a sorted unique `capabilities` list with
      `changeset.v1`.
- [ ] Legacy ack without capabilities remains valid for scene.query only.
- [ ] Malformed capability lists fail closed.
- [ ] Client refuses preflight without capability and sends no frame.
- [ ] Existing scene.query request/response wire compatibility is preserved.
- [ ] Preflight parser rejects unknown fields, duplicate keys, wrong tags,
      digest/manifest/binding mismatch, and oversized payloads.

## Fact integrity

- [ ] DTOs are frozen/slotted, canonical, bounded, and deterministic.
- [ ] Every affected/read/operation reference has a fact or an explicit
      structured failure; no target is silently omitted.
- [ ] Stable node ID is resolved before path and ambiguity fails closed.
- [ ] Mirrored workspace/node identity, path, type, parent, capability, role,
      lock, parm, wire, scene epoch, and manifest revision are independently
      checked from current scene facts.
- [ ] Results include no HOM proxy, callable, arbitrary dict operation, token,
      traceback, or raw parameter/source dump.
- [ ] Condition results and `all_preconditions_hold` are derived from facts,
      not Runtime policy claims.

## Main-thread/no-write review

- [ ] `scene.query` and preflight use one bounded FIFO.
- [ ] FIFO ordering, queue capacity, deadline, cancellation and shutdown
      semantics remain unchanged for existing reads.
- [ ] All HOM access happens inside the queue pump callable.
- [ ] Mutation-spy/fake-scene tests prove no create/delete/set/connect,
      undo/save/load/clear/HDA/file/shell/code operation occurs.
- [ ] Old server fails closed for preflight; no capability means no request.

## Independent commands

```powershell
uv lock --check
uv run --extra eval pytest tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py -q
uv run --extra eval pytest -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check <parent>..<commit>
git status --short --branch
```

- [ ] Focused/full tests pass with only the pre-existing WSL skip.
- [ ] Compile, lock and diff checks pass.
- [ ] No unauthorized files or local state are present.

## Promotion

Any capability bypass, stale-fact acceptance, incomplete fact coverage,
mutation, queue interleaving, token leak, unauthorized file, or new test
failure is blocking. Only after acceptance may the typed executor (16-D) and
workspace lifecycle seam (B2b) be planned.
