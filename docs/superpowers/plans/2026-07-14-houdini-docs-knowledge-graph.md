# Houdini Documentation Knowledge Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, local, read-only Houdini 21.0.440 documentation knowledge graph with SQLite/FTS5, exact symbol resolution, bounded Agent tools, and evidence-backed corpus evaluation.

**Architecture:** Parse local Houdini zip documents and repository skills into frozen drafts, reconcile SOP document candidates against a hython-generated NodeType inventory, resolve aliases and one-hop graph edges, and atomically materialize a versioned SQLite cache. A LangChain-independent `KnowledgeService` owns status/search/get behavior; two thin read-only tool adapters expose it to the existing Foundation agent while preserving live Houdini introspection as the final authority.

**Tech Stack:** Python 3.11, standard-library `sqlite3` with FTS5, `zipfile`, `hashlib`, `subprocess`, frozen dataclasses and enums, LangChain Core tools, pytest 8.4.1, uv, Houdini/hython 21.0.440 for explicitly marked local contract tests.

---

## Execution Policy

- Work only in `E:\eee-agent\.worktrees\knowledge-graph` on branch `feature/houdini-knowledge-graph`.
- Read `CLAUDE.md`, the approved spec, and the current stage in this plan before editing.
- Execute exactly one stage at a time. Stop after its commits and report evidence; do not begin the next stage until Codex audits and approves it.
- Use test-driven development for every behavior: write a focused failing test, run it and record the expected failure, implement the minimum behavior, rerun the focused test, then run the stage suite and full regression.
- Preserve all user changes. Never reset, restore, or rewrite unrelated files.
- Do not import `hou`, Runtime, Qt, rpyc bridge modules, model providers, or third-party retrieval libraries from `eee_agent.knowledge`.
- Do not add dependencies or modify `uv.lock`. `pyproject.toml` may change only in Task 14 to register the `houdini_kb` pytest marker.
- Do not implement automatic cache building, online crawling, embeddings, vector search, Runtime integration, or skill activation.
- Do not commit generated SideFX text, a generated SQLite cache, raw hython inventory, API keys, absolute user paths, or test output.
- Each task has its own commit. Do not squash task commits before review.
- A stage completion report must contain: branch, commit SHAs, changed files, red/green commands and outputs, full regression result, `git status --short`, deviations, and remaining risks.

## Authoritative Documents

- Design: `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`
- Project context: `CLAUDE.md`
- Current tool registry: `eee_agent/tools/registry.py`
- Current live node introspection: `eee_agent/tools/nodes.py::describe_node_type`
- Current prompt assembly: `eee_agent/system_prompt.py`

## Stage Map

| Stage | Tasks | Deliverable | Stop condition |
|---|---|---|---|
| 1 | 1–2 | Frozen graph contracts and common Creole parser | Common parser tests and full regression pass |
| 2 | 3–5 | SOP, HOM, VEX and skill domain parsers | Synthetic domain corpus parses without ID loss |
| 3 | 6–8 | Hython inventory, node reconciliation, graph resolution and manifest | Deterministic in-memory graph bundle passes invariants |
| 4 | 9–10 | SQLite schema/writer and atomic explicit builder | Selftest builds and validates a temporary cache |
| 5 | 11–12 | Read-only store and KnowledgeService | Exact/ambiguous/FTS/get/error/budget contracts pass |
| 6 | 13 | Config, LangChain tools, registry and prompt integration | Compiled Agent contains both read-only KB tools |
| 7 | 14–15 | Real HFS contract tests and golden retrieval evaluation | 21.0.440 build and retrieval thresholds pass |
| 8 | 16 | Documentation, handoff and final verification | Clean branch with complete evidence, no Runtime merge |

## File Map

### Create

- `eee_agent/knowledge/__init__.py`: stable public exports only.
- `eee_agent/knowledge/models.py`: enums and immutable parser/graph DTOs.
- `eee_agent/knowledge/ids.py`: entity ID and source-path normalization.
- `eee_agent/knowledge/parse_common.py`: BOM, metadata, title, summary, section and reference parsing.
- `eee_agent/knowledge/parse_node.py`: SOP node-document parser and candidate derivation.
- `eee_agent/knowledge/parse_hom.py`: HOM page and method parser.
- `eee_agent/knowledge/parse_vex.py`: VEX function parser.
- `eee_agent/knowledge/parse_skill.py`: repository skill parser.
- `eee_agent/knowledge/inventory.py`: HFS discovery and hython SOP NodeType inventory.
- `eee_agent/knowledge/graph.py`: alias collection, node reconciliation, reference resolution and graph invariants.
- `eee_agent/knowledge/manifest.py`: source fingerprints and deterministic manifest hash.
- `eee_agent/knowledge/schema.py`: SQLite schema version and schema creation.
- `eee_agent/knowledge/writer.py`: graph/manifest materialization and integrity checks.
- `eee_agent/knowledge/sources.py`: safe zip/skill source loading.
- `eee_agent/knowledge/lock.py`: exclusive build lock with stale-lock rules.
- `eee_agent/knowledge/build.py`: explicit CLI, selftest and atomic build orchestration.
- `eee_agent/knowledge/store.py`: low-level read-only SQLite queries.
- `eee_agent/knowledge/api.py`: search/get/status request and response contracts.
- `eee_agent/knowledge/service.py`: status, exact symbol, filtered FTS, neighbors and bounded get.
- `eee_agent/tools/knowledge.py`: two LangChain tool adapters.
- `tests/knowledge/__init__.py`: test package marker.
- `tests/knowledge/test_models.py`: enum/DTO/ID contracts.
- `tests/knowledge/test_parse_common.py`: common document grammar.
- `tests/knowledge/test_parse_node.py`: SOP identity/version/namespace parsing.
- `tests/knowledge/test_parse_hom.py`: HOM page and method parsing.
- `tests/knowledge/test_parse_vex_skill.py`: VEX and skill parsing.
- `tests/knowledge/test_inventory.py`: HFS discovery, subprocess and reconciliation.
- `tests/knowledge/test_graph.py`: aliases, ambiguity, edges and invariants.
- `tests/knowledge/test_manifest.py`: deterministic fingerprints and hash.
- `tests/knowledge/test_schema.py`: schema, indexes, FTS and writer.
- `tests/knowledge/test_builder.py`: safe sources, lock, atomic replace and selftest.
- `tests/knowledge/test_store.py`: read-only SQL and search primitives.
- `tests/knowledge/test_service.py`: public service behavior and budgets.
- `tests/knowledge/test_tools.py`: tool adapters and stable errors.
- `tests/knowledge/test_hfs_contract.py`: opt-in real 21.0.440 corpus contract.
- `tests/test_agent_knowledge_contract.py`: compiled tool and prompt contract.
- `eval/knowledge/golden_queries.yaml`: 30–50 expected retrieval cases without copyrighted bodies.
- `eval/knowledge/run_eval.py`: retrieval metrics and threshold command.
- `docs/handoffs/2026-07-14-houdini-knowledge-graph.md`: final branch evidence and Runtime merge contract.

### Modify

- `eee_agent/config.py`: strict `KnowledgeConfig` and local cache/HFS path resolution.
- `eee_agent/tools/registry.py`: register exactly two knowledge tools.
- `eee_agent/system_prompt.py`: add the approved compact query/introspection rule.
- `.env.example`: document `EEE_KB_ENABLED`, `EEE_KB_PATH`, and `EEE_HFS`.
- `.gitignore`: ignore a project-local `.knowledge-cache/` override.
- `pyproject.toml`: register only the opt-in `houdini_kb` pytest marker in Task 14; dependency lists remain unchanged.
- `README.md`: document explicit build/query commands after implementation is verified.
- `CLAUDE.md`: record KB architecture, source authority and cache gotchas after verification.
- `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`: change status to implemented only in Stage 8.

## Stable Contracts Used By Every Stage

The names below are fixed by this plan. If implementation evidence requires a change, stop and request a design amendment before renaming them.

```python
# eee_agent/knowledge/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class Authority(StrEnum):
    OFFICIAL_HOUDINI_DOCS = "official_houdini_docs"
    PROJECT_VERIFIED_SKILL = "project_verified_skill"


class EntityKind(StrEnum):
    NODE_DOCUMENT = "node_document"
    VEX_FUNCTION = "vex_function"
    HOM_CLASS = "hom_class"
    HOM_METHOD = "hom_method"
    HOM_FUNCTION = "hom_function"
    HOM_MODULE = "hom_module"
    HOM_PACKAGE = "hom_package"
    SKILL_REFERENCE = "skill_reference"


class OperatorTypeStatus(StrEnum):
    VERIFIED_AT_BUILD = "verified_at_build"
    DOCUMENTED_UNVERIFIED = "documented_unverified"
    AMBIGUOUS = "ambiguous"
    HISTORICAL_ONLY = "historical_only"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class SectionDraft:
    key: str
    heading: str
    body: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class ReferenceDraft:
    display_text: str | None
    target_kind: str
    raw_target: str
    normalized_target: str
    anchor: str | None
    source_line: int


@dataclass(frozen=True, slots=True)
class EntityDraft:
    entity_id: str
    kind: EntityKind
    subtype: str
    canonical_name: str
    title: str
    summary: str
    authority: Authority
    source_path: str
    source_anchor: str | None
    is_current: bool
    attributes: Mapping[str, object] = field(default_factory=dict)
    body: str = ""
    sections: tuple[SectionDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class AliasDraft:
    alias: str
    entity_id: str
    alias_type: str
    priority: int


@dataclass(frozen=True, slots=True)
class EdgeDraft:
    source_id: str
    predicate: str
    target_id: str | None
    target_raw: str | None
    target_anchor: str | None
    resolved: bool
    source_location: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    entities: tuple[EntityDraft, ...]
    aliases: tuple[AliasDraft, ...]
    edges: tuple[EdgeDraft, ...]


@dataclass(frozen=True, slots=True)
class GraphBundle:
    entities: tuple[EntityDraft, ...]
    aliases: tuple[AliasDraft, ...]
    edges: tuple[EdgeDraft, ...]
```

---

## Stage 1: Contracts And Common Parsing

### Task 1: Frozen Models And Entity IDs

**Files:**
- Create: `eee_agent/knowledge/__init__.py`
- Create: `eee_agent/knowledge/models.py`
- Create: `eee_agent/knowledge/ids.py`
- Create: `tests/knowledge/__init__.py`
- Create: `tests/knowledge/test_models.py`

- [ ] **Step 1: Write failing model and ID tests**

Create tests that assert the exact enum values above and these ID/path rules:

```python
import pytest

from eee_agent.knowledge.ids import make_entity_id, normalize_source_path
from eee_agent.knowledge.models import EntityKind


def test_source_path_is_logical_posix_and_rejects_escape() -> None:
    assert normalize_source_path(r"sop\apex--buildfkgraph.txt") == (
        "sop/apex--buildfkgraph.txt"
    )
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("../nodes.zip")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("C:/houdini/help/nodes.zip")


def test_entity_id_is_stable_and_kind_prefixed() -> None:
    assert make_entity_id(
        EntityKind.NODE_DOCUMENT,
        "sop/apex--buildfkgraph.txt@current",
    ) == "node_document:sop/apex--buildfkgraph.txt@current"
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
uv run --extra eval pytest tests/knowledge/test_models.py -v
```

Expected: collection fails because `eee_agent.knowledge` does not exist.

- [ ] **Step 3: Implement the stable contracts**

Implement `models.py` exactly as the Stable Contracts block. Implement `ids.py` with `PurePosixPath`; replace backslashes, reject empty/absolute/drive/`.`/`..` paths, and require non-empty ID keys without ASCII controls. `make_entity_id` returns `f"{kind.value}:{key}"` without case-folding.

`__init__.py` exports only enums and DTOs; it must not open a database or locate HFS at import time.

- [ ] **Step 4: Run GREEN and regression**

```powershell
uv run --extra eval pytest tests/knowledge/test_models.py -v
uv run --extra eval pytest -q
```

Expected: focused tests pass; full suite has no failures.

- [ ] **Step 5: Commit Task 1**

```powershell
git add eee_agent/knowledge tests/knowledge
git commit -m "feat: define knowledge graph contracts"
```

### Task 2: Common Creole Parser

**Files:**
- Create: `eee_agent/knowledge/parse_common.py`
- Create: `tests/knowledge/test_parse_common.py`

- [ ] **Step 1: Write failing grammar tests**

Use synthetic text covering BOM, metadata continuation, multiline summary, duplicate headings and references:

```python
from eee_agent.knowledge.parse_common import (
    decode_source,
    parse_metadata,
    parse_references,
    parse_sections,
    parse_summary,
    parse_title,
)


SOURCE = '''\ufeff= Example =
#type: node
#tags: model,
    polygons
"""A short
summary."""
== Overview == (overview)
See [Boolean SOP|Node:sop/boolean] and [Hom:hou.Node#createNode].
:include _common#geometry:
== Overview == (advanced)
Advanced text.
'''


def test_common_document_fields_and_sections() -> None:
    text = decode_source(SOURCE.encode("utf-8"))
    assert parse_title(text) == "Example"
    assert parse_metadata(text)["tags"] == "model,\npolygons"
    assert parse_summary(text) == "A short summary."
    assert [section.key for section in parse_sections(text)] == [
        "overview", "advanced"
    ]


def test_labeled_anchor_and_include_references() -> None:
    refs = parse_references(decode_source(SOURCE.encode("utf-8")))
    assert [(r.target_kind, r.normalized_target, r.anchor) for r in refs] == [
        ("Node", "sop/boolean", None),
        ("Hom", "hou.Node", "createNode"),
        ("Include", "_common", "geometry"),
    ]
```

Also test `[Vex:intersect]`, `[node()|#node]`, invalid UTF-8, and summary absence.

- [ ] **Step 2: Run tests and verify RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_common.py -v
```

Expected: import fails because `parse_common.py` does not exist.

- [ ] **Step 3: Implement common parsing**

Implement the pure, I/O-free functions `decode_source(data: bytes) -> str`, `parse_metadata(text: str) -> dict[str, str]`, `parse_title(text: str) -> str`, `parse_summary(text: str) -> str`, `parse_sections(text: str) -> tuple[SectionDraft, ...]`, `parse_references(text: str) -> tuple[ReferenceDraft, ...]`, and `clean_body(text: str) -> str`.

Implementation rules:

- Decode only `utf-8-sig`; raise `UnicodeDecodeError` unchanged.
- Metadata continuation accepts indented non-directive lines until the next unindented directive/content line.
- Collapse summary whitespace to single spaces.
- Prefer an explicit heading anchor in parentheses; otherwise slugify the heading. Resolve duplicate section keys by the explicit anchor, then stable `-2`, `-3` suffixes.
- Parse labeled typed references before direct typed references so the inner target is not emitted twice.
- Parse local anchors and include directives separately.
- Record one-based source line numbers.
- `clean_body` removes `:fig:`, video/image-only directives and repeated blank lines, but preserves headings, signatures, arguments and prose.

- [ ] **Step 4: Run GREEN and regression**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_common.py -v
uv run --extra eval pytest tests/knowledge -q
uv run --extra eval pytest -q
```

- [ ] **Step 5: Commit Task 2**

```powershell
git add eee_agent/knowledge/parse_common.py tests/knowledge/test_parse_common.py
git commit -m "feat: parse Houdini document grammar"
```

### Stage 1 Claude Code Prompt

```text
Work in E:\eee-agent\.worktrees\knowledge-graph on branch feature/houdini-knowledge-graph.

Read CLAUDE.md, docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md sections 3, 6 and 7, and Stage 1 of docs/superpowers/plans/2026-07-14-houdini-docs-knowledge-graph.md. Use superpowers:test-driven-development and superpowers:verification-before-completion.

Implement Stage 1 only: Task 1 immutable models/entity IDs and Task 2 common Creole parsing. Follow the exact names and enum values in the plan. Do not implement any domain parser, SQLite, builder, service, tool, config, prompt, Runtime integration, or dependency change. Do not use real SideFX document bodies in tests; use the synthetic fixtures specified by the plan.

For each task, demonstrate RED before implementation, GREEN after implementation, run the full regression, and create the exact task commit. Stop after Task 2. Report branch, commit SHAs, changed files, red/green evidence, full pytest result, git status, deviations and risks. Do not begin Stage 2.
```

### Stage 1 Codex Audit Gate

- Only Task 1–2 files changed.
- `eee_agent.knowledge` imports perform no I/O.
- DTO and enum names match the stable contracts.
- IDs preserve case and distinguish document identity from operator type.
- Parser handles labeled references without duplicate direct-reference emission.
- No official document excerpt is committed.
- Focused tests and complete suite pass from a clean worktree.

---

## Stage 2: Domain Parsers

### Task 3: SOP Node Documents

**Files:**
- Create: `eee_agent/knowledge/parse_node.py`
- Create: `tests/knowledge/test_parse_node.py`

- [ ] **Step 1: Write failing SOP identity tests**

Create minimal fixtures for `sop/apex--buildfkgraph.txt`, `sop/loadslices.txt`, `sop/agentlookat-2.0.txt`, `sop/agentlookat.txt` and `sop/agentlookat-.txt`. Assert:

```python
def test_namespace_filename_beats_incorrect_internal_metadata() -> None:
    parsed = parse_node_document(
        "sop/apex--buildfkgraph.txt",
        (
            "#type: node\n#context: sop\n#namespace: apex\n"
            "#internal: graph\n= APEX Build FK Graph =\n\n"
            "\"\"\"Builds a graph.\"\"\""
        ),
    )
    entity = parsed.entities[0]
    assert entity.entity_id == (
        "node_document:sop/apex--buildfkgraph.txt@current"
    )
    assert entity.attributes["operator_type_candidates"] == (
        "apex::buildfkgraph", "graph"
    )


def test_historical_versions_have_distinct_ids() -> None:
    ids = {
        parse_node_document(path, text).entities[0].entity_id
        for path, text in THREE_AGENTLOOKAT_FIXTURES
    }
    assert len(ids) == 3
```

Assert current status, version fields, source aliases, internal metadata as low-priority alias, tags/context facets in attributes, and unresolved reference edges.

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_node.py -v
```

- [ ] **Step 3: Implement `parse_node_document`**

Implement the exact public signature `parse_node_document(source_path: str, text: str) -> ParsedDocument`.

Derive the source slug from the basename. Only a final `-` or a suffix matching `-[0-9]+\.[0-9]+` denotes a historical page; namespace `--` is not a version separator. Candidate order is versioned filename-derived operator, unversioned filename-derived operator, then `#internal`. Deduplicate while preserving order. A page without a historical filename suffix is current even when `#version` is present. Do not mark any candidate verified in the parser.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_node.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/parse_node.py tests/knowledge/test_parse_node.py
git commit -m "feat: parse SOP node documents"
```

### Task 4: HOM Pages And First-Class Methods

**Files:**
- Create: `eee_agent/knowledge/parse_hom.py`
- Create: `tests/knowledge/test_parse_hom.py`

- [ ] **Step 1: Write failing HOM tests**

Use synthetic class/function/module/include fixtures. The class fixture must contain two `::` method blocks, one repeated method name with a second signature, `#cppname`, return references and a superclass. Assert:

```python
def test_hom_methods_are_entities_and_overloads_are_aggregated() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    by_id = {entity.entity_id: entity for entity in parsed.entities}
    assert "hom_class:hou.Node" in by_id
    method = by_id["hom_method:hou.Node#createNode"]
    assert method.attributes["owner"] == "hou.Node"
    assert len(method.attributes["signatures"]) == 2
    assert any(edge.predicate == "declares_method" for edge in parsed.edges)


def test_hom_function_and_class_case_are_distinct() -> None:
    klass = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    func = parse_hom_document("hou/node_.txt", HOM_NODE_FUNCTION_FIXTURE)
    assert klass.entities[0].entity_id != func.entities[0].entity_id
```

Assert that `include` produces no entity, six page types route correctly, and method block boundaries stop at the next method/section.

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_hom.py -v
```

- [ ] **Step 3: Implement `parse_hom_document`**

Implement the exact public signature `parse_hom_document(source_path: str, text: str) -> ParsedDocument`.

Aggregate repeated method blocks by `(owner, method_name)`, preserve signature order, use a stable overload ordinal only when two independent blocks cannot be aggregated, create `declares_method` and `inherits_from` edges, and emit exact qualified/short/casefold aliases with distinct priorities.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_hom.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/parse_hom.py tests/knowledge/test_parse_hom.py
git commit -m "feat: parse HOM entities and methods"
```

### Task 5: VEX Functions And Project Skills

**Files:**
- Create: `eee_agent/knowledge/parse_vex.py`
- Create: `eee_agent/knowledge/parse_skill.py`
- Create: `tests/knowledge/test_parse_vex_skill.py`

- [ ] **Step 1: Write failing VEX/skill tests**

Assert all `:usage:` signatures, returns, context/group/tags, related edges, include exclusion, skill frontmatter removal, repository-relative path, SHA-256 attribute and authority separation.

```python
def test_vex_signatures_and_related_edges() -> None:
    parsed = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE)
    entity = parsed.entities[0]
    assert entity.entity_id == "vex_function:intersect_all"
    assert len(entity.attributes["signatures"]) == 2
    assert any(edge.predicate == "related_to" for edge in parsed.edges)


def test_skill_has_project_authority_and_content_hash() -> None:
    parsed = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE)
    entity = parsed.entities[0]
    assert entity.authority.value == "project_verified_skill"
    assert len(entity.attributes["sha256"]) == 64
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_vex_skill.py -v
```

- [ ] **Step 3: Implement both pure parsers**

Implement the exact public signatures `parse_vex_document(source_path: str, text: str) -> ParsedDocument` and `parse_skill_document(source_path: str, text: str) -> ParsedDocument`.

VEX include/suite/context pages return an empty `ParsedDocument`. Skill links create edges only for explicit typed reference syntax; plain prose does not infer edges.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_parse_vex_skill.py -v
uv run --extra eval pytest tests/knowledge -q
uv run --extra eval pytest -q
git add eee_agent/knowledge/parse_vex.py eee_agent/knowledge/parse_skill.py tests/knowledge/test_parse_vex_skill.py
git commit -m "feat: parse VEX documentation and skills"
```

### Stage 2 Claude Code Prompt

```text
Continue in E:\eee-agent\.worktrees\knowledge-graph on feature/houdini-knowledge-graph only after Stage 1 is audited. Read the approved spec sections 5–7 and Stage 2 of the implementation plan. Use test-driven-development and verification-before-completion.

Implement Tasks 3–5 only: SOP document identity/candidates, HOM pages plus first-class methods, and VEX/skill parsing. Preserve the Stage 1 public contracts. Use only synthetic/minimal rewritten fixtures. Do not add inventory, graph resolution, SQLite, service, tools, config, dependencies, real HFS tests or Runtime code.

Demonstrate RED/GREEN per task, make the exact three commits, run tests/knowledge and the full suite, then stop. Report commit SHAs, changed files, evidence, clean status, deviations and risks. Do not begin Stage 3.
```

### Stage 2 Codex Audit Gate

- Current/historical SOP IDs are unique and namespace-safe.
- `#internal` remains low-priority metadata and never becomes verified here.
- Every HOM method is queryable as an entity; overloads do not overwrite.
- Case-sensitive `hou.Node`/`hou.node` identities remain distinct.
- VEX and skill authority/edge rules match the spec.
- No I/O in domain parsers and no copyrighted fixture bodies.

---

## Stage 3: Inventory, Graph Resolution And Manifest

### Task 6: HFS Discovery And Hython Inventory

**Files:**
- Create: `eee_agent/knowledge/inventory.py`
- Create: `tests/knowledge/test_inventory.py`

- [ ] **Step 1: Write failing discovery/subprocess tests**

Test priority `--hfs argument > EEE_HFS > HFS > known paths`, required files, no absolute path persistence, subprocess argument list without `shell=True`, clean JSON-only stdout, timeout and non-zero exit errors. Inject the subprocess runner and filesystem probes.

```python
def test_inventory_uses_hython_without_shell(tmp_path: Path) -> None:
    calls = []
    inventory = load_sop_inventory(
        make_fake_hfs(tmp_path),
        runner=lambda args, **kwargs: record_success(calls, args, kwargs),
    )
    assert inventory == frozenset({"apex::buildfkgraph", "loadslices"})
    assert calls[0][1]["shell"] is False
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_inventory.py -v
```

- [ ] **Step 3: Implement inventory**

Expose `resolve_hfs(explicit: str | None, environ: Mapping[str, str], candidates: tuple[Path, ...]) -> Path` and `load_sop_inventory(hfs: Path, *, runner=subprocess.run, timeout_seconds: int = 30) -> frozenset[str]`.

The hython `-c` program imports `hou`, gets `hou.sopNodeTypeCategory().nodeTypes().values()`, outputs sorted unique `type.name()` JSON, and emits no absolute paths. Reject malformed JSON and non-string list members.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_inventory.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/inventory.py tests/knowledge/test_inventory.py
git commit -m "feat: inventory Houdini SOP node types"
```

### Task 7: Node Reconciliation And Graph Resolution

**Files:**
- Create: `eee_agent/knowledge/graph.py`
- Create: `tests/knowledge/test_graph.py`

- [ ] **Step 1: Write failing graph tests**

Build a synthetic multi-document corpus and assert:

- preferred versioned candidate wins when present;
- missing candidates become unresolved;
- historical-only pages never become current aliases;
- equal top-priority aliases return multiple candidates;
- labeled Node, VEX, HOM method anchor, superclass and related refs resolve;
- unresolved and ambiguous targets remain explicit;
- duplicate entity IDs and dangling resolved edges fail invariants.

```python
def test_reconcile_prefers_versioned_verified_operator() -> None:
    bundle = assemble_graph(
        NODE_DOCUMENTS,
        inventory=frozenset({"boolean::2.0", "polyextrude::2.0"}),
    )
    boolean = entity(bundle, "node_document:sop/boolean.txt@current")
    assert boolean.attributes["operator_type"] == "boolean::2.0"
    assert boolean.attributes["operator_type_status"] == "verified_at_build"
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_graph.py -v
```

- [ ] **Step 3: Implement graph assembly**

Expose `assemble_graph(documents: tuple[ParsedDocument, ...], *, inventory: frozenset[str]) -> GraphBundle` and `validate_graph(bundle: GraphBundle) -> None`.

Resolve in two passes: collect entities/aliases, then edges. Alias lookup returns all maximum-priority candidates. Node candidate verification respects parser order and exact inventory membership. Sort final tuples deterministically by stable keys.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_graph.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/graph.py tests/knowledge/test_graph.py
git commit -m "feat: reconcile and resolve knowledge graph"
```

### Task 8: Deterministic Build Manifest

**Files:**
- Create: `eee_agent/knowledge/manifest.py`
- Create: `tests/knowledge/test_manifest.py`

- [ ] **Step 1: Write failing deterministic hash tests**

```python
def test_manifest_hash_excludes_time_and_absolute_paths() -> None:
    first = build_manifest(created_at_utc="2026-07-14T01:00:00Z", **INPUTS)
    second = build_manifest(created_at_utc="2026-07-14T02:00:00Z", **INPUTS)
    assert first.manifest_sha256 == second.manifest_sha256
    assert "E:\\" not in first.canonical_payload
```

Also assert source archive/skill/inventory hash changes alter the manifest and counts are sorted.

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_manifest.py -v
```

- [ ] **Step 3: Implement fingerprints and manifest**

Use frozen `SourceFingerprint` and `BuildManifest` dataclasses. Canonical JSON uses `sort_keys=True`, `separators=(",", ":")`, UTF-8 and logical paths. Hash deterministic payload excluding `created_at_utc` and `manifest_sha256`; store both in the final serializable metadata.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_manifest.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/manifest.py tests/knowledge/test_manifest.py
git commit -m "feat: fingerprint knowledge graph builds"
```

### Stage 3 Claude Code Prompt

```text
Continue only after Stage 2 audit. Work in the knowledge-graph worktree/branch. Read spec sections 6.3, 7, 8.3 and 9.1 plus Stage 3 of the plan. Use TDD and verification-before-completion.

Implement Tasks 6–8 only: injected/testable HFS+hython inventory, deterministic two-pass graph reconciliation/resolution, and deterministic manifest hashing. Do not read real HFS in default tests. Do not add SQLite, builder orchestration, service, tools, config, prompt or Runtime code.

Make the exact three commits, show RED/GREEN and full regression, then stop. Report subprocess safety, graph invariant evidence, manifest determinism evidence, commit SHAs, clean status, deviations and risks. Do not begin Stage 4.
```

### Stage 3 Codex Audit Gate

- `subprocess.run` uses an argument list, timeout, captured text and `shell=False`.
- Hython output is validated and no absolute HFS path enters graph/manifest.
- Node candidate order and historical behavior match spec.
- Resolved edges cannot dangle; ambiguity is explicit.
- Manifest hash excludes time and machine paths and is reproducible.

---

## Stage 4: SQLite Cache And Explicit Builder

### Task 9: Schema And Deterministic Writer

**Files:**
- Create: `eee_agent/knowledge/schema.py`
- Create: `eee_agent/knowledge/writer.py`
- Create: `tests/knowledge/test_schema.py`

- [ ] **Step 1: Write failing schema/writer tests**

Assert schema version 1, foreign keys, required indexes, `facets`, FTS5 availability, deterministic rows, metadata, body/section persistence, resolved target integrity and query-only reopen.

```python
def test_schema_contains_graph_facets_and_fts(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
        )}
    assert {"entities", "aliases", "facets", "edges", "documents",
            "sections", "entities_fts"} <= names
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_schema.py -v
```

- [ ] **Step 3: Implement schema/writer**

`schema.py` exports `KB_SCHEMA_VERSION = 1`, `create_schema(conn)` and `verify_fts5(conn)`. Use parameterized inserts, `PRAGMA foreign_keys=ON`, explicit transaction, unique constraints for aliases/facets/sections and indexes specified by the design.

`writer.py` exports `write_cache(path: Path, bundle: GraphBundle, manifest: BuildManifest) -> None` and `validate_cache(path: Path) -> None`.

Extract `context`, `tag`, `group`, `superclass` and operator status into `facets`; retain the complete attributes JSON. Populate FTS using canonical name/title/summary/tags/body. Derive `edge_id` as SHA-256 of canonical JSON containing source, predicate, target/raw target, anchor and source location. `validate_cache` checks `integrity_check == "ok"`, `foreign_key_check` empty, schema version, entity counts and resolved edges.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_schema.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/schema.py eee_agent/knowledge/writer.py tests/knowledge/test_schema.py
git commit -m "feat: write SQLite knowledge graph cache"
```

### Task 10: Safe Sources, Build Lock And Atomic CLI

**Files:**
- Create: `eee_agent/knowledge/sources.py`
- Create: `eee_agent/knowledge/lock.py`
- Create: `eee_agent/knowledge/build.py`
- Create: `tests/knowledge/test_builder.py`

- [ ] **Step 1: Write failing source/lifecycle tests**

Test safe logical zip entries, rejection of absolute/`..`, exact three zips/four skills, exclusive lock, active lock rejection, stale lock recovery only when PID is absent and age exceeds the constant, atomic replacement, failure preservation, temp cleanup and `--selftest` without HFS.

```python
def test_failed_build_preserves_previous_cache(tmp_path: Path) -> None:
    final = tmp_path / "knowledge.sqlite3"
    final.write_bytes(b"previous-valid-cache")
    with pytest.raises(BuildError):
        build_cache(BuildOptions(output=final), writer=raising_writer)
    assert final.read_bytes() == b"previous-valid-cache"
    assert not list(tmp_path.glob("knowledge.sqlite3.tmp.*"))
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_builder.py -v
```

- [ ] **Step 3: Implement sources/lock/build**

Define this options contract:

```python
@dataclass(frozen=True, slots=True)
class BuildOptions:
    output: Path
    hfs: Path | None = None
    selftest: bool = False
```

Expose `build_cache(options: BuildOptions, *, now: Callable[[], datetime], pid_exists: Callable[[int], bool]) -> BuildManifest` and `main(argv: Sequence[str] | None = None) -> int`. Production defaults for `now` and `pid_exists` are bound by named wrapper functions, while tests pass deterministic callables explicitly.

Use `os.open(lock, O_CREAT|O_EXCL|O_WRONLY)`, JSON lock ownership, same-directory temp DB and `os.replace`. Never extract zip entries. Dispatch parsers by logical archive/path/type. `main` prints one JSON summary to stdout, sanitized errors to stderr, and returns non-zero on failure. `--selftest` constructs the plan's synthetic corpus and temporary inventory.

- [ ] **Step 4: Run GREEN, CLI selftest and regression**

```powershell
uv run --extra eval pytest tests/knowledge/test_builder.py -v
uv run python -m eee_agent.knowledge.build --selftest
uv run --extra eval pytest -q
```

Expected: selftest JSON contains schema version, manifest hash, entity/edge counts and a temporary output that was validated and cleaned.

- [ ] **Step 5: Commit Task 10**

```powershell
git add eee_agent/knowledge/sources.py eee_agent/knowledge/lock.py eee_agent/knowledge/build.py tests/knowledge/test_builder.py
git commit -m "feat: build knowledge cache atomically"
```

### Stage 4 Claude Code Prompt

```text
Continue only after Stage 3 audit. Read spec sections 8, 9 and 14 plus Stage 4 of the plan. Implement Tasks 9–10 only using TDD.

Build the exact SQLite schema including facets/FTS, deterministic parameterized writer, safe zip/skill loaders, exclusive stale-aware lock, atomic explicit builder and HFS-free --selftest. Do not implement read queries/service/tools/config/prompt, real HFS contracts, eval, Runtime integration, dependencies or automatic building.

Prove failed builds preserve the old cache and unsafe zip paths are rejected. Make the exact two commits, run focused tests, --selftest and full pytest, then stop with the required evidence. Do not begin Stage 5.
```

### Stage 4 Codex Audit Gate

- Schema includes facets and all required indexes/constraints.
- Writer uses explicit transaction and parameterized SQL.
- No zip extraction or path traversal.
- Lock ownership and stale recovery cannot delete another active build.
- Atomic failure test proves old cache preservation.
- `--selftest` requires neither HFS nor RPC and leaves no cache in repo.

---

## Stage 5: Read-Only Store And KnowledgeService

### Task 11: Read-Only Store Primitives

**Files:**
- Create: `eee_agent/knowledge/store.py`
- Create: `tests/knowledge/test_store.py`

- [ ] **Step 1: Write failing store tests**

Use the Stage 4 fixture cache. Assert URI `mode=ro`, `query_only=ON`, metadata/status, exact alias candidates at maximum priority, facets, safe FTS tokenization, incoming/outgoing neighbors, section lookup and inability to mutate.

```python
def test_store_is_physically_read_only(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        with pytest.raises(sqlite3.OperationalError):
            store.connection.execute("DELETE FROM entities")
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_store.py -v
```

- [ ] **Step 3: Implement store**

`KnowledgeStore.open(path)` uses `sqlite3.connect(f"file:{quoted}?mode=ro", uri=True)`, row factory and `PRAGMA query_only=ON`. Expose typed internal rows for metadata, exact alias candidates, filtered entities, FTS candidates, document/section and neighbors. Build FTS queries from tokens matched by `[A-Za-z0-9_:.]+`; quote every token and join with `OR`. User input never becomes SQL syntax or an identifier.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_store.py -v
uv run --extra eval pytest -q
git add eee_agent/knowledge/store.py tests/knowledge/test_store.py
git commit -m "feat: query knowledge cache read only"
```

### Task 12: Service API, Errors And Budgets

**Files:**
- Create: `eee_agent/knowledge/api.py`
- Create: `eee_agent/knowledge/service.py`
- Create: `tests/knowledge/test_service.py`

- [ ] **Step 1: Write failing service tests**

Define and test `KnowledgeErrorCode`, `KnowledgeStatus`, `SearchRequest`, `GetRequest`, response serialization, `INVALID_ARGUMENT`, `KB_NOT_BUILT`, active adjacent build lock as `KB_BUILDING`, schema mismatch/corrupt/stale, `NO_MATCH`, `UNKNOWN_ENTITY`, exact match, ambiguous candidates, combined filters, FTS ranking, incoming/outgoing predicate-filtered one-hop relations, provenance, historical default and all response caps.

```python
def test_ambiguous_symbol_never_selects_silently(service: KnowledgeService) -> None:
    response = service.search(SearchRequest(symbol="hou.node"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.AMBIGUOUS_SYMBOL
    assert len(response.candidates) == 2


def test_get_enforces_hard_character_limit(service: KnowledgeService) -> None:
    response = service.get(GetRequest(entity_id=LONG_ENTITY, max_chars=99_999))
    assert response.ok is True
    assert len(response.body) <= 8_000
    assert response.truncated is True
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_service.py -v
```

- [ ] **Step 3: Implement API/service**

Use frozen request/response dataclasses with `to_dict()`. Construct `KnowledgeService(path: Path, *, stale_checker: Callable[[Mapping[str, object]], bool])`; callers must supply the checker, and tests inject deterministic true/false checkers. The service evaluates it once per instance, checks the adjacent build lock before opening the cache, and accepts only `outgoing`/`incoming` directions. `predicate`/`direction` are valid only when `symbol` resolves uniquely. `SearchRequest.limit` clamps to 25; get clamps to 8,000 and truncates at the last paragraph boundary when possible. Search result summary ≤240, tags ≤12, signatures ≤5, neighbors ≤8, candidates ≤10. Every success/error contains sanitized KB provenance when metadata is readable. Catch only expected filesystem/SQLite/domain failures and map them to explicit codes; unexpected exceptions become `INTERNAL_ERROR` without traceback/SQL/path.

- [ ] **Step 4: Run GREEN/regression and commit**

```powershell
uv run --extra eval pytest tests/knowledge/test_service.py -v
uv run --extra eval pytest tests/knowledge -q
uv run --extra eval pytest -q
git add eee_agent/knowledge/api.py eee_agent/knowledge/service.py tests/knowledge/test_service.py
git commit -m "feat: serve bounded Houdini knowledge queries"
```

### Stage 5 Claude Code Prompt

```text
Continue only after Stage 4 audit. Read spec sections 10–12 and Stage 5. Implement Tasks 11–12 only with TDD: physically read-only SQLite store primitives and the LangChain-independent KnowledgeService/API with exact ambiguity, filtered FTS, one-hop neighbors, sections, provenance, stable errors and hard output budgets.

Do not add tools, registry, prompt/config changes, real HFS build, eval, docs, Runtime code or dependencies. Never interpolate user text into SQL. Make the exact two commits, show focused and full test evidence, then stop. Do not begin Stage 6.
```

### Stage 5 Codex Audit Gate

- Database cannot be mutated through the store.
- FTS builder accepts only normalized tokens and SQL remains parameterized.
- Ambiguous symbols cannot return a normal match.
- All counts and character caps are enforced inside service, not just tools.
- Errors contain no traceback, SQL or absolute HFS path.
- Service imports no LangChain, bridge, Runtime or provider module.

---

## Stage 6: Foundation Agent Integration

### Task 13: Strict Config, Tools, Registry And Prompt

**Files:**
- Modify: `eee_agent/config.py`
- Create: `eee_agent/tools/knowledge.py`
- Modify: `eee_agent/tools/registry.py`
- Modify: `eee_agent/system_prompt.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `tests/knowledge/test_tools.py`
- Create: `tests/test_agent_knowledge_contract.py`
- Modify: `tests/test_env_example.py`

- [ ] **Step 1: Write failing config/tool/agent tests**

Assert strict booleans, path resolution, no import-time DB open, stable tool names/schemas, service monkeypatching, tools never raise, registry contains exactly 27 project tools including both KB tools, compiled graph has KB tools and no `task`, prompt contains the approved rule once, and `get_houdini_knowledge` is not in `READBACK_TOOLS`.

```python
def test_knowledge_tools_are_registered_without_task(monkeypatch) -> None:
    configure_test_agent_env(monkeypatch)
    graph = build_agent()
    names = set(graph.nodes["tools"].bound._tools_by_name)
    assert {"search_houdini_knowledge", "get_houdini_knowledge"} <= names
    assert "task" not in names
```

- [ ] **Step 2: Run RED**

```powershell
uv run --extra eval pytest tests/knowledge/test_tools.py tests/test_agent_knowledge_contract.py tests/test_env_example.py -v
```

- [ ] **Step 3: Implement strict config and adapters**

Add this exact config contract and a `knowledge_config() -> KnowledgeConfig` factory:

```python
@dataclass(frozen=True)
class KnowledgeConfig:
    enabled: bool
    path: str
    hfs: str | None
```

Default cache path uses `%LOCALAPPDATA%\EEEAgent\cache\knowledge\houdini\21.0.440\knowledge.sqlite3`; tests set `LOCALAPPDATA`. Relative `EEE_KB_PATH` resolves against `repo_root()`. Reuse `_env_bool`.

`eee_agent/tools/knowledge.py` exposes exactly the two spec signatures, including predicate/direction on search, and a lazy `_service()` factory. The factory supplies a source-fingerprint stale checker built from `KnowledgeConfig.hfs`; when no current HFS can be resolved it reports only verified schema/corruption state and does not invent staleness. Disabled state returns `KB_DISABLED`. All exceptions are converted to sanitized dicts. Register both in `ALL_TOOLS` without changing existing order. Add only the approved compact prompt paragraph and do not trim either tool by name.

- [ ] **Step 4: Update environment/ignore documentation**

Document commented variables in `.env.example` and add `.knowledge-cache/` to `.gitignore`. Do not add a live cache path inside `eee_agent/`.

- [ ] **Step 5: Run GREEN, dependency guard and regression**

```powershell
uv run --extra eval pytest tests/knowledge/test_tools.py tests/test_agent_knowledge_contract.py tests/test_env_example.py -v
uv lock --check
git diff --exit-code -- pyproject.toml uv.lock
uv run --extra eval pytest -q
```

- [ ] **Step 6: Commit Task 13**

```powershell
git add eee_agent/config.py eee_agent/tools/knowledge.py eee_agent/tools/registry.py eee_agent/system_prompt.py .env.example .gitignore tests/knowledge/test_tools.py tests/test_agent_knowledge_contract.py tests/test_env_example.py
git commit -m "feat: expose Houdini knowledge tools"
```

### Stage 6 Claude Code Prompt

```text
Continue only after Stage 5 audit. Read spec sections 4, 11–14 and Stage 6. Implement Task 13 only: strict KnowledgeConfig, two thin lazy LangChain tools, all_tools registration, the exact compact prompt rule, environment docs and local cache ignore.

Preserve existing 25 tools and harness behavior. Do not add tools to READBACK_TOOLS, do not build a cache at import/query time, do not change dependencies, CLI modes, Runtime code, skills or provider code. Tests must monkeypatch the service and require no HFS/RPC/model call.

Show RED/GREEN, uv lock guard and full regression, make the exact commit, then stop. Report compiled ToolNode names and clean status. Do not begin Stage 7.
```

### Stage 6 Codex Audit Gate

- Tool module is a thin adapter and opens nothing at import.
- Exactly two new project tools, no implicit `task`.
- Existing tool order/contracts remain intact.
- Prompt rule appears once and preserves live introspection priority.
- `READBACK_TOOLS` unchanged for knowledge get.
- `pyproject.toml` and `uv.lock` unchanged.

---

## Stage 7: Real Corpus Contract And Retrieval Evaluation

### Task 14: Houdini 21.0.440 Corpus Build Contract

**Files:**
- Create: `tests/knowledge/test_hfs_contract.py`
- Modify: `pyproject.toml` only to register a pytest marker; do not change dependencies.

- [ ] **Step 1: Register and write opt-in HFS tests**

Add marker configuration:

```toml
markers = [
    "houdini_kb: requires local Houdini 21.0.440 HFS and hython",
]
```

Tests skip unless `EEE_RUN_HOUDINI_KB_TESTS=true`. When enabled, resolve HFS, build to `tmp_path`, assert audited ranges and facts:

```python
assert 1_100 <= counts["node_document"] <= 1_130
assert 1_080 <= counts["vex_function"] <= 1_120
assert 330 <= counts["hom_class"] <= 350
assert 5_300 <= counts["hom_method"] <= 5_650
assert verified_operator("apex::buildfkgraph")
assert verified_operator("loadslices")
assert verified_operator("kinefx::rigpython")
```

Also assert every `verified_at_build` operator is in inventory, resolved targets exist, manifest records unresolved/ambiguous/historical counts, and no source absolute path is stored.

- [ ] **Step 2: Run default skip and explicit local contract**

```powershell
uv run --extra eval pytest tests/knowledge/test_hfs_contract.py -v
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
$env:EEE_HFS='D:\houdini'
uv run --extra eval pytest tests/knowledge/test_hfs_contract.py -m houdini_kb -v
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
```

On the other machine use the verified C drive HFS path. Expected: default run skips; explicit run passes and writes only under pytest temp.

- [ ] **Step 3: Run explicit production-style build**

```powershell
$out = Join-Path $env:TEMP 'eee-houdini-kb-review.sqlite3'
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out $out
uv run python -c "from pathlib import Path; from eee_agent.knowledge.writer import validate_cache; validate_cache(Path(r'$out')); print('valid')"
Remove-Item -LiteralPath $out
```

- [ ] **Step 4: Commit Task 14**

```powershell
git add pyproject.toml tests/knowledge/test_hfs_contract.py
git commit -m "test: verify Houdini knowledge corpus"
```

### Task 15: Golden Retrieval Evaluation

**Files:**
- Create: `eval/knowledge/golden_queries.yaml`
- Create: `eval/knowledge/run_eval.py`

- [ ] **Step 1: Write 30–50 golden cases**

Each YAML case contains only query metadata and expected IDs/symbols, not source bodies:

```yaml
- id: vex_intersect_all
  request:
    query: ray intersect geometry all hits
    kinds: [vex_function]
  expected_any:
    - vex_function:intersect_all
  unambiguous: true
```

Cover all categories specified by design §15.4, including explicit ambiguous cases.

- [ ] **Step 2: Write evaluator tests before evaluator**

Add evaluator unit tests in `eval/knowledge/test_run_eval.py` or `tests/knowledge/test_eval.py` for top-1, recall@5, MRR, ambiguity correctness, response sizes and p95 calculation using fake responses. Run and verify RED.

- [ ] **Step 3: Implement evaluator**

`run_eval.py` accepts `--kb`, loads YAML with existing PyYAML, executes service requests, emits deterministic JSON metrics, and returns non-zero unless:

- unambiguous exact-symbol top-1 = 100%;
- recall@5 ≥ 95%;
- ambiguous correctness = 100%;
- every response respects service hard caps.

- [ ] **Step 4: Run evaluator and regression**

```powershell
uv run --extra eval pytest tests/knowledge/test_eval.py -v
$out = Join-Path $env:TEMP 'eee-houdini-kb-eval.sqlite3'
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out $out
uv run --extra eval python eval/knowledge/run_eval.py --kb $out
Remove-Item -LiteralPath $out
uv lock --check
uv run --extra eval pytest -q
```

- [ ] **Step 5: Commit Task 15**

```powershell
git add eval/knowledge tests/knowledge/test_eval.py
git commit -m "test: evaluate knowledge retrieval quality"
```

### Stage 7 Claude Code Prompt

```text
Continue only after Stage 6 audit. Read spec sections 5, 15 and 16 plus Stage 7. Implement Tasks 14–15 only: opt-in real Houdini 21.0.440 corpus contracts and a 30–50 case golden retrieval evaluator.

The default suite must not require HFS. Use pytest tmp paths and remove the manual review cache. Golden YAML contains IDs/requests only, no official document bodies. Register only the pytest marker in pyproject; do not change dependencies or uv.lock. Do not modify parser/service/tool behavior merely to game metrics; if a genuine retrieval defect appears, stop and report it for a focused reviewed fix rather than hiding it in eval code.

Run default skip, explicit HFS contract, production-style temp build, evaluator, uv lock check and full regression. Make the exact two commits and stop. Report corpus counts, unresolved/ambiguous counts, manifest hash, metrics, p95, response sizes, commits and clean status. Do not begin Stage 8.
```

### Stage 7 Codex Audit Gate

- Default tests remain HFS-independent.
- Real build uses the local 21.0.440 HFS and no RPC/GUI.
- Audited counts are ranges, not a single brittle total.
- Verified operator invariant holds.
- Golden cases cover every entity category and ambiguity.
- Metrics meet thresholds without query-specific hardcoding.
- No generated cache/raw docs/inventory are tracked.

---

## Stage 8: Documentation, Handoff And Final Verification

### Task 16: Operational Documentation And Branch Handoff

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md`
- Create: `docs/handoffs/2026-07-14-houdini-knowledge-graph.md`

- [ ] **Step 1: Update verified user/developer documentation**

Document only commands demonstrated in Stage 7:

- explicit build and `--selftest`;
- cache location and overrides;
- search/get tool roles;
- document-vs-live-Houdini authority;
- cache rebuild rule and no-git rule;
- real contract/eval commands;
- no Runtime integration on this branch.

Change design status to `已实现（独立分支，待 Runtime 集成）` only after all prior gates pass.

- [ ] **Step 2: Write handoff evidence**

The handoff records branch base/head, task commits, file map, exact verification outputs, corpus counts, manifest hash, metrics, known limitations and the Runtime merge checklist:

```text
read_only_tools allowlist
RuntimePaths shared cache
startup KB status event
Run snapshot manifest/schema/build
restricted Research Capability
combined Runtime + Knowledge contract tests
```

- [ ] **Step 3: Run final verification**

```powershell
uv lock --check
uv run python -m eee_agent.knowledge.build --selftest
uv run --extra eval pytest -q
$env:EEE_RUN_HOUDINI_KB_TESTS='true'
$env:EEE_HFS='D:\houdini'
uv run --extra eval pytest tests/knowledge/test_hfs_contract.py -m houdini_kb -v
Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
$out = Join-Path $env:TEMP 'eee-houdini-kb-final.sqlite3'
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out $out
uv run --extra eval python eval/knowledge/run_eval.py --kb $out
Remove-Item -LiteralPath $out
git diff --check
git status --short
```

Expected: all checks pass; status lists only the four intended documentation files before commit; generated cache is absent from git status.

- [ ] **Step 4: Commit Task 16**

```powershell
git add README.md CLAUDE.md docs/superpowers/specs/2026-07-14-houdini-docs-knowledge-graph.md docs/handoffs/2026-07-14-houdini-knowledge-graph.md
git commit -m "docs: hand off Houdini knowledge graph"
```

- [ ] **Step 5: Verify clean committed branch**

```powershell
git status --short --branch
git log --oneline --decorate feature/foundation..HEAD
git diff --stat feature/foundation...HEAD
```

### Stage 8 Claude Code Prompt

```text
Continue only after Stage 7 audit. Implement Stage 8 Task 16 only. Update README, CLAUDE, design status and the branch handoff using only commands and evidence already verified. Do not modify implementation/tests, merge/rebase Runtime, push, create a PR, or claim Runtime integration.

Run every final verification command, create the exact documentation commit, and stop. Report the complete branch commit list, test/corpus/eval evidence, diff stat, clean status, known limitations and Runtime merge checklist. Do not merge or push.
```

### Stage 8 Codex Audit Gate

- Documentation matches executable commands and observed output.
- Design status does not overclaim Runtime integration.
- Handoff contains every task commit and evidence.
- Full/default/HFS/eval/lock checks pass freshly.
- Worktree is clean and no generated/copyrighted/cache file is tracked.
- Branch is ready for a separate Runtime integration decision, not silently merged.

---

## Codex Review Protocol After Every Claude Code Stage

After the user reports a stage complete, Codex must independently:

1. Confirm branch/worktree and inspect `git status`, stage commits and diff against the previously approved commit.
2. Read every changed file; do not rely on Claude Code's summary.
3. Compare changes line by line with the stage tasks and audit gate.
4. Check for unrelated edits, dependency changes, generated data, absolute paths, copied official bodies and hidden scope expansion.
5. Verify the reported RED failure is credible and the GREEN test targets the same behavior.
6. Rerun focused stage tests and the full regression; for Stages 7–8 also rerun the HFS/eval commands.
7. Run `git diff --check`, dependency/lock guards and repository searches relevant to the stage.
8. Return exactly one verdict:
   - `通过`：all requirements and evidence pass; provide the next-stage prompt.
   - `需修正`：bounded defects; provide a dedicated Claude Code repair prompt and withhold the next stage.
   - `阻断`：design/security/data-contract conflict; stop and request a design amendment.

Claude Code must never receive the next stage prompt before the current stage verdict is `通过`.

## Spec Coverage Matrix

| Design requirement | Implemented/verified by |
|---|---|
| Local four-source scope and authority | Tasks 3–5, 10, 14 |
| Document ID separate from operator type | Tasks 1, 3, 6–7 |
| First-class HOM methods and overloads | Task 4; Tasks 7, 12 and 14 verify retrieval |
| Typed references, aliases, ambiguity and one-hop graph | Tasks 2, 7, 11–12 |
| SQLite entities/aliases/facets/edges/documents/sections/FTS | Task 9 |
| Deterministic fingerprints and provenance | Tasks 8–10, 12 |
| Safe HFS/hython inventory | Tasks 6, 10 and 14 |
| Lock, atomic replace and failure preservation | Task 10 |
| Read-only storage and bounded service | Tasks 11–12 |
| Tool/config/prompt and live introspection authority | Task 13 |
| No dependency or Runtime expansion | Global guards, Tasks 13–16 |
| Real corpus invariants and retrieval thresholds | Tasks 14–15 |
| Operational documentation and Runtime merge contract | Task 16 |

Self-review found no design requirement that lacks an implementation task or verification gate. Runtime integration remains intentionally outside this branch and is documented only as a later merge contract.

## Final Branch Acceptance

The independent branch is complete only when:

- Tasks 1–16 exist as reviewed commits.
- Design §16 acceptance criteria are all mapped to passing tests/evidence.
- Full unit suite, explicit HFS contract and golden eval pass freshly.
- `uv.lock` remains in sync and no new dependency was added.
- No generated KB, SideFX source text, raw inventory, absolute user path or secret is tracked.
- Knowledge tools are read-only, bounded and present in the Foundation Agent without `task`.
- Live `describe_node_type` remains authoritative for actual node availability/parameters.
- Worktree is clean and Runtime integration remains an explicit later operation.
