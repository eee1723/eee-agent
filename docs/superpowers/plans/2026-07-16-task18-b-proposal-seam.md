# Task 18-B Modeling Proposal Seam Plan

**Status:** Pure seam accepted locally after 99 focused and 2140 full
offline tests. Runtime graph integration is not started.

**Goal:** Add a pure trusted proposal coordinator and bounded LangChain tool
adapter on top of the accepted Task 18-A compiler.

**Design:** `docs/superpowers/specs/2026-07-16-task18-b-proposal-seam-design.md`

## Authorized files

- Create `eee_agent/modeling/proposal.py`
- Create `tests/modeling/test_proposal.py`
- Add proposal exports to `eee_agent/modeling/__init__.py`
- Add this plan/spec and a review result

Do not modify Runtime service/runner/server, panel, protocol, tool registry,
ChangeSet contracts/policy, Bridge, Houdini adapter, provider, dependencies,
or lockfile.

## Acceptance commands

```powershell
uv run --frozen --extra eval pytest tests/modeling -q
uv run --frozen --extra eval pytest -q
uv run --frozen --extra eval python -m compileall -q eee_agent houdini_side tests
uv lock --check
git diff --check
```

The existing exact read-only AgentRunner/tool registry tests must remain green.
No live Houdini test is required for this pure seam.
