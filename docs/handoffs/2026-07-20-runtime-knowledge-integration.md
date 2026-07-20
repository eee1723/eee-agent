# Runtime Knowledge Graph integration handoff

Date: 2026-07-20
Branch: `feature/runtime`

## Baselines and tags

Before selective transplant, the following annotated tags were created locally
(not pushed):

| tag | annotated object | peeled commit |
| --- | --- | --- |
| `foundation-final-2026-07-17` | `0f63ef46174e196ba5bb1790f537865233d52245` | `b9ef66f07cb001ef97c0191dc7305fabd9921fa4` |
| `knowledge-graph-final-2026-07-17` | `9021ef80316fd9dbe3ec5b4e7406739d125a8837` | `7004eadd954e1b765c36baab513402c4fe04cc5f` |
| `runtime-pre-knowledge-integration-2026-07-17` | `757cebc7af8a619914a2ca17d4d1d5e5eed92b02` | `d5415514c6be11ea436fa48bbeb19c8a92beea36` |

The Knowledge branch was **not merged**. Its history contains the retired
bridge/tools/panel paths, so only the files listed below were restored from it.

## Selected transplant

- `eee_agent/knowledge/**`: parser, manifest, read-only store and service.
- `tests/knowledge/**` except the legacy `test_tools.py` adapter, which was
  removed because it imports the retired `eee_agent.tools` registry.
- `eval/knowledge/**` and Knowledge golden query data.
- `tests/runtime/test_knowledge_integration.py`: Runtime boundary and failure
  tests.
- `eee_agent/runtime/knowledge.py`: bounded Runtime facade.
- `eee_agent/runtime/paths.py`: user-local shared cache path outside Runtime
  state/session/artifact directories.
- `eee_agent/runtime/agent_context.py`, `agent_tools.py`, and `service.py`:
  secure knowledge allowlist, provider composition and run snapshot fields.
- `pyproject.toml`: opt-in `houdini_kb` marker.

No Runtime module imports `eee_agent.bridge` or `eee_agent.tools`; no model
input can access a SQLite connection, filesystem path, or raw document body
outside the bounded `get_houdini_knowledge` response.

## Runtime contract

`RuntimePaths.knowledge_cache_path` resolves to the user-local
`knowledge-cache/houdini.sqlite` sibling of `state`, while app DB,
checkpoints, artifacts and session cleanup remain under `state`.

`KnowledgeRuntime` classifies cache state as exactly `missing`, `stale`,
`corrupt`, `schema_mismatch`, or `ready`. All states allow Runtime startup;
non-ready search/get calls return a bounded `kb_unavailable` result. Ready
provenance is frozen into every new Run snapshot as:

- `kb_manifest_sha256`
- `kb_schema_version`
- `houdini_build`
- `knowledge_status`

Knowledge text does not grant creatability. The live catalog remains the only
authority for modeling capabilities.

## Verification

- Focused Runtime Knowledge integration: `8 passed`.
- Runtime agent tools/runner focused suite: `42 passed`.
- Runtime service/runs focused suite: `167 passed`.
- Knowledge service focused suite: `48 passed`.
- Full repository gate: **2807 passed, 11 skipped** in 149.03s. The 11 skips
  are the opt-in HFS contract cases, skipped because
  `EEE_RUN_HOUDINI_KB_TESTS` was not set for the normal gate.
- Explicit HFS gate with `EEE_RUN_HOUDINI_KB_TESTS=true`:
  **11 passed** (`tests/knowledge/test_hfs_contract.py`).
- `uv lock --check`, `compileall`, and `git diff --check` passed. No Runtime or
  Knowledge source imports `eee_agent.bridge` or `eee_agent.tools`.

## Branch cleanup

Local and remote `feature/foundation` and `feature/houdini-knowledge-graph`
remain until the combined Runtime + Knowledge test gates and HFS contract
gate pass. Keep `main`, `wip/pre-migration-main`, and `feature/runtime`.
Before deletion, copy exact branch SHAs and these tags into the final cleanup
commit. The approved cleanup targets are exactly:

- local worktrees `E:\eee-agent\.worktrees\foundation` and
  `E:\eee-agent\.worktrees\knowledge-graph`;
- local branches `feature/foundation` and
  `feature/houdini-knowledge-graph`;
- remote refs `origin/feature/foundation` and
  `origin/feature/houdini-knowledge-graph`.

The following are explicitly retained: `main`, `wip/pre-migration-main`,
`feature/runtime`, all three annotated baseline tags above, and the root
worktree `E:\eee-agent`.

Pre-cleanup remote branch SHAs recorded for audit:

- `origin/feature/foundation` → `b9ef66f07cb001ef97c0191dc7305fabd9921fa4`
- `origin/feature/houdini-knowledge-graph` →
  `7004eadd954e1b765c36baab513402c4fe04cc5f`
