# Handoff — Houdini Documentation Knowledge Graph

- **Branch:** `feature/houdini-knowledge-graph`
- **Base:** `b9ef66f` (tip of `feature/foundation`); the knowledge-graph branch
  does **not** depend on `feature/runtime`.
- **Pre-Stage-8 HEAD:** `97e0ebb` (Stage 7).
- **Final HEAD:** the Stage 8 `docs: hand off Houdini knowledge graph` commit
  (this commit) — resolve with `git rev-parse HEAD`.
- **Design status:** changed to `已实现（独立分支，待 Runtime 集成）` in
  `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`.
- **Scope:** an offline, read-only knowledge graph over the docs that ship in the
  local Houdini 21.0.440 install (SOP nodes, VEX functions, `hou.*`
  classes/functions/methods) plus the 4 project skills — materialized into a
  single versioned SQLite + FTS5 cache and exposed through two bounded, read-only
  Agent tools. **No Runtime integration on this branch.**

## What was built

A deterministic build pipeline (HFS discovery → hython SOP inventory → archive
parsing → two-pass graph reconciliation → deterministic manifest → atomic SQLite
write) plus a LangChain-independent `KnowledgeService` (status/search/get/
neighbors) and two thin LangChain tool adapters. The query path is purely
read-only (`mode=ro`, `query_only=ON`), never imports the rpyc bridge or `hou`,
and never raises into the Agent. Live `describe_node_type()` remains the final
authority for actual node creatability and parameters.

## Task commits (Stages 1–8)

Task commits (the rest of the branch commits are reviewed fixes between tasks):

| Stage | Task | Commit | Subject |
|---|---|---|---|
| 1 | 1 | `5f11ff7` | feat: define knowledge graph contracts |
| 1 | 2 | `e761073` | feat: parse Houdini document grammar |
| 2 | 3 | `80125bd` | feat: parse SOP node documents |
| 2 | 4 | `38fe56d` | feat: parse HOM entities and methods |
| 2 | 5 | `03b5bd4` | feat: parse VEX documentation and skills |
| 3 | 6 | `9c86a3f` | feat: inventory Houdini SOP node types |
| 3 | 7 | `2277221` | feat: reconcile and resolve knowledge graph |
| 3 | 8 | `5fd85b6` | feat: fingerprint knowledge graph builds |
| 4 | 9 | `e9daadb` | feat: write SQLite knowledge graph cache |
| 4 | 10 | `9ea77ba` | feat: build knowledge cache atomically |
| 5 | 11 | `d5b1623` | feat: query knowledge cache read only |
| 5 | 12 | `366494c` | feat: serve bounded Houdini knowledge queries |
| 6 | 13 | `bf0cd54` | feat: expose Houdini knowledge tools |
| 7 | 14 | `a8bf3b2` | test: verify Houdini knowledge corpus |
| 7 | 15 | `97e0ebb` | test: evaluate knowledge retrieval quality |
| 8 | 16 | *(this commit)* | docs: hand off Houdini knowledge graph |

Supporting reviewed fixes on the branch (not task commits): `bcaa229`,
`8832ace`, `6e99a2a`, `5e4c81f`, `a1e7fe7`, `a7fd24d` (parsers); `bcdf4c9`,
`0dba40f`, `a1ee224` (graph/manifest); `c966bd8`, `ba56f20`, `e84185e`,
`5940d22` (schema/builder); `3bca8ca` (service); plus branch setup
`5b54e5b`, `6f57249`, `05b50a8`.

## File map by stage

- **Stage 1 (contracts + common parser):** `eee_agent/knowledge/{__init__,models,ids,parse_common}.py`; `tests/knowledge/{__init__,test_models,test_parse_common}.py`
- **Stage 2 (domain parsers):** `eee_agent/knowledge/{parse_node,parse_hom,parse_vex,parse_skill}.py`; `tests/knowledge/{test_parse_node,test_parse_hom,test_parse_vex_skill}.py`
- **Stage 3 (inventory + graph + manifest):** `eee_agent/knowledge/{inventory,graph,manifest}.py`; `tests/knowledge/{test_inventory,test_graph,test_manifest}.py`
- **Stage 4 (schema/writer + builder):** `eee_agent/knowledge/{schema,writer,sources,lock,build}.py`; `tests/knowledge/{test_schema,test_builder}.py`
- **Stage 5 (store + service):** `eee_agent/knowledge/{store,api,service}.py`; `tests/knowledge/{test_store,test_service}.py`
- **Stage 6 (agent integration):** `eee_agent/{config.py,tools/knowledge.py,tools/registry.py,system_prompt.py}`; `.env.example`; `.gitignore`; `tests/knowledge/test_tools.py`; `tests/test_agent_knowledge_contract.py`; `tests/test_env_example.py`
- **Stage 7 (corpus contract + evaluator):** `tests/knowledge/test_hfs_contract.py`; `pyproject.toml` (pytest `houdini_kb` marker only — **no dependency change**); `eval/knowledge/{golden_queries.yaml,run_eval.py}`; `tests/knowledge/test_eval.py`
- **Stage 8 (this commit — docs only):** `README.md`; `CLAUDE.md`; `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`; `docs/handoffs/2026-07-14-houdini-knowledge-graph.md`

No implementation/test file was modified in Stage 8. `pyproject.toml`/`uv.lock`
are unchanged from Stage 7 (deps unchanged across the whole branch — `rpyc==4.1.0`
etc. as locked on Foundation).

## Verified evidence (final run, 2026-07-15)

Commands actually run (PowerShell, machine B, `D:\houdini`). The single final
production build below (`--out $out`) is the sole source of the final hashes,
counts and evaluator metrics recorded in this handoff — nothing is mixed from a
different HFS/input snapshot:

```powershell
uv lock --check
uv run python -m eee_agent.knowledge.build --selftest
uv run --extra eval pytest -q

$env:EEE_RUN_HOUDINI_KB_TESTS = 'true'
$env:EEE_HFS = 'D:\houdini'
uv run --extra eval pytest tests/knowledge/test_hfs_contract.py -m houdini_kb -v
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
Remove-Item Env:EEE_HFS -ErrorAction SilentlyContinue

$out = Join-Path $env:TEMP 'eee-houdini-kb-final-doc-audit.sqlite3'
Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out $out
uv run python -c "from pathlib import Path; from eee_agent.knowledge.writer import validate_cache; validate_cache(Path(r'$out')); print('valid')"
uv run --extra eval python eval/knowledge/run_eval.py --kb $out
Remove-Item -LiteralPath $out -Force
```

- **`uv lock --check`:** in sync — `Resolved 66 packages`.
- **`--selftest`:** `{"ok": true, "selftest": true, "validated": true, ...,
  "temp_cleaned": true}` (synthetic corpus; manifest_sha256
  `50653bafdc73d69d6e1aa4ecdc05991fbb95b40d6611874b593655cbb6c5ebfa`).
- **Full default regression (`pytest -q`):** **831 passed, 11 skipped**. The 11
  skips are the opt-in `houdini_kb` HFS contract tests — the default suite is
  HFS-independent.
- **Focused counts:** Task 13 contract `32 passed`; Task 15 evaluator `23 passed`.
- **Default HFS skip:** `pytest tests/knowledge/test_hfs_contract.py -v` →
  **11 skipped** (reason: `set EEE_RUN_HOUDINI_KB_TESTS=true`).
- **Explicit HFS contract** (PowerShell:
  `$env:EEE_RUN_HOUDINI_KB_TESTS='true'`; `$env:EEE_HFS='D:\houdini'`;
  `pytest tests/knowledge/test_hfs_contract.py -m houdini_kb -v`): **11 passed**.
- **Final production build + `validate_cache`:** → `valid` (this is the build the
  hashes/counts/metrics below are read from).

### Corpus facts (21.0.440)

- **Houdini version / build:** `21.0.440`
- **Entity counts (total 8476):** node_document **1115**, vex_function **1099**,
  hom_class **339**, hom_method **5368**, hom_function 342, hom_module 203,
  hom_package 6, skill_reference 4.
- **Edge counts (total 26131):** references 14598, declares_method 5368,
  includes 3442, related_to 2575, inherits_from 148.
- **Unresolved references:** **6027** · **ambiguous aliases:** **694** (both > 0
  and recorded in the manifest).
- **Verified operators (`verified_at_build`, each confirmed in the hython SOP
  inventory):** `apex::buildfkgraph`, `loadslices`, `kinefx::rigpython`.
- Invariants also asserted: 0 dangling resolved edges; exact top-level
  `sop/*.txt` node source scope; the 4 project skills (`project_verified_skill`);
  no stored absolute machine path (URL-safe regex over decoded attribute values).

### Final build hash — target-input-snapshot-specific (NOT universally reproducible)

The manifest hash is a fingerprint of the **exact build input snapshot**, not a
universal constant. It is reproduced only when the source snapshot below matches;
a different SOP inventory (different installed SOP operators / HDAs / config), a
different zip byte-packaging of the same docs, or a different Houdini patch level
yields a **different** manifest hash while preserving the same parsed entity
counts (the documentation content is identical even when the byte fingerprint
differs). Two consecutive fresh builds from this worktree against `D:\houdini`
reproduce deterministically **within this environment**:

- **manifest_sha256:** `e4c6e29d0699808cb10053e01dd37be75894781bb3f72b873ce2807a5824e295`
- **node_inventory_sha256:** `7d3e4a9492f45b0dff54b5bdae5dd3c1ae35e6a3ba8ea5ae6811c1a0d1a26b1f`

Auditable source snapshot this hash corresponds to (read from the same cache's
manifest `source_archives`, `skill_sources` and `node_inventory_sha256`):

| input | sha256 |
|---|---|
| `nodes.zip` (5,944,063 B) | `f3f03b8ed9a9cbf0297a8d5ef14d64099402a2cf05cde226ddcefcb2fac80143` |
| `hom.zip` (1,130,218 B) | `f60653a4a635025852fad34446ae2b8f3c052c1e1fb34198e64a413337436921` |
| `vex.zip` (684,257 B) | `5d69040c8aebd626a3022abca01c09711cc7ac64073f28c879b9519e9e23742d` |
| `skills/parametric-building/SKILL.md` | `8f4a4076480f1d259c8620ba9bc8835a097058450691b77fc7cd5d0e4bcf3827` |
| `skills/procedural-components/SKILL.md` | `7b259f4b3b423fbd6fb56cf421f89c71a875aed0207789b4340253458eb4cd3e` |
| `skills/sop-cookbook/SKILL.md` | `d498c8be45b3257ce7619b35fe04fecbadbbd8848b7733fccd6cad4317deb10c` |
| `skills/vex-patterns/SKILL.md` | `e27c3dcfd9367ac666d9e520d088f4986dc7f5104a2e794e3f8b717b53caf9e2` |

**Reproducibility scope:** this hash is **not** claimed to reproduce on a machine
whose input snapshot differs, and it must not be described as a universally
reproducible final-build hash. The audit environment (also `D:\houdini`, also
`21.0.440`) reproduces **deterministically but differently** as manifest
`04d766a741530128a60f7ef90b45bf2aebcaf450392e4cabb70d92b19142a3fd` / inventory
`d6e1c9b23db9df020213ee15f321320b5a67992b9796f2b4bd81cc073fc9f6c3` — with the
**same** counts (entities 8476, edges 26131, unresolved 6027, ambiguous 694) and
`validate_cache=valid`. That divergence (identical parsed content, different
manifest hash) is precisely why the hash is qualified here as snapshot-specific.
The counts, `validate_cache=valid`, and the evaluator metrics recorded below are
all read from this same single cache.

### Golden evaluator metrics (`run_eval.py --kb <final cache>`)

| metric | value | gate |
|---|---|---|
| cases (match / exact-symbol) | 46 (38 / 35) | — |
| exact_top1 | **1.0** | =100% ✓ |
| recall_at_5 | **1.0** | ≥95% ✓ |
| MRR | 1.0 | reported |
| ambiguity_correctness | **1.0** | =100% ✓ |
| caps_ok | **True** | ✓ |
| max_response_chars | **3364** | — |
| p95_latency_ms | **510.4 ms (≈ 0.51 s)** | reported (no threshold) |
| passes | **True** (exit 0) | — |

Zero recall / top-1 / ambiguity misses. p95 is run-dependent and is dominated by
the service's per-query `PRAGMA integrity_check` (Stage 5 design, not modifiable
here); it is reported, not gated.

## Qualifications

- **Official-body example-path qualification:** entity source paths, edge
  provenance, metadata, service provenance and error output contain ONLY logical
  POSIX source paths (`sop/boolean.txt`, `hou/Node.txt`,
  `skills/vex-patterns/SKILL.md`, …) — never build-machine absolute paths, and no
  source absolute path is stored in entities, edges, metadata, provenance or
  error output. Note: official SideFX document *bodies* (held only inside the
  cache) may legitimately contain example Windows paths such as `C:/temp/...` —
  that is official body content, not build-machine provenance, and it is never
  indexed or reported as a source path. No official SideFX document body is
  committed to git or shipped as a source artifact; bodies live only in the
  machine-local, gitignored, rebuildable SQLite cache. The golden YAML and
  evaluator output carry only request metadata, expected entity IDs and metrics
  — never document bodies.
- **Stale-checker scope qualification:** the lazy service's stale checker
  re-fingerprints ONLY the three HFS source archives (`nodes/hom/vex.zip`) and
  reports `kb_stale` on a sha256 mismatch with the stored manifest. It does NOT
  re-run the hython inventory, does NOT fingerprint the four skill sources, and
  if no current HFS can be resolved (or any read fails) it returns **False** — it
  never invents staleness. Schema version, integrity and corruption are verified
  separately by the service on every open. Consequently an inventory-only change
  (archives unchanged) would not be flagged stale by this checker (acceptable:
  the archives capture the document set; the inventory is derived from them).

## Known limitations

- No automatic build; an explicit `python -m eee_agent.knowledge.build` is
  required (queries otherwise return `kb_not_built`).
- No embeddings / vector / semantic search — FTS5 token-based retrieval only; no
  multi-hop graph algorithms.
- v1 does not index HScript expression/command docs, images, or videos.
- `#internal` is an alias only — never a node primary key or an unverified
  executable type; query results carry `operator_type` + `operator_type_status`
  and the agent must still confirm via `describe_node_type`.
- Query latency p95 ~0.5 s, dominated by the per-query `PRAGMA integrity_check`
  (Stage 5 design).
- No Runtime integration on this branch (see checklist below).
- Inherited Foundation limitation: DeepSeek V4 Pro over-iterates on long-horizon
  tasks; the knowledge tools do not change that (Claude remains the recommended
  reliability swap via `EEE_LLM_PROVIDER=anthropic`).

## Runtime merge checklist (NOT done on this branch)

These are merge contract items for a separate, reviewed integration — they are
intentionally not implemented here:

- [ ] **`read_only_tools` allowlist:** add `search_houdini_knowledge` and
      `get_houdini_knowledge` to the Runtime read-only tool allowlist.
- [ ] **RuntimePaths shared cache:** bring the
      `%LOCALAPPDATA%\EEEAgent\cache\knowledge\houdini\21.0.440\knowledge.sqlite3`
      path under RuntimePaths while preserving its "shared, rebuildable cache"
      semantics (it is NOT a per-Run business artifact).
- [ ] **Startup KB status event:** Runtime preflight checks KB status
      (missing / stale / corrupt / schema-mismatch) and emits a structured
      status event; it must NOT block Runtime startup.
- [ ] **Run snapshot:** record `kb_manifest_sha256`, `kb_schema_version` and
      `houdini_build` in each Run snapshot.
- [ ] **Restricted Research Capability:** Supervisor / Modeling capability uses
      `KnowledgeService` through a bounded Research Capability; the Executor must
      not read arbitrary document bodies directly.
- [ ] **Combined contract tests:** both the Knowledge contract suite and the
      Runtime contract suite must pass together before merge (resolve
      `registry.py`, config paths, read-only allowlist and Run-snapshot
      intersections).

## Final branch state

- Worktree clean at HEAD; no generated cache, raw SideFX text, hython inventory,
  absolute user path or secret is tracked.
- `uv.lock` in sync; no new runtime dependency was added on this branch.
- `live describe_node_type` remains authoritative for actual node
  availability/parameters.
- Branch is ready for a separate Runtime integration decision — it is **not**
  silently merged and **not** pushed.
