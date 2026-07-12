# EEE Agent — Fix Plan (context bloat, loops, observability)

## STATUS (2026-07-12)
**Implemented + unit/hython-verified** (all on `main`, committed):
- **Phase 0 — component port architecture** (`eee_agent/tools/procedural.py`):
  - `make_component`: `geo_port`(0) + `anchors_port`(1) via internal `output` nodes
    with explicit `outputidx` (vanilla-subnet multi-output — VERIFIED in 21.0.440;
    a vanilla subnet otherwise has one effective output).
  - `wire_anchor`: real `consumer.setInput(in, producer, anchors_port)` wire +
    `in_anchors_<prod>` tap via `indirectInputs()` — **replaces object_merge**; cycle-checked.
  - `assemble_output`: real `merge` of every component's `geo_port` (no object_merge) →
    work subnet auto-layouts by DAG.
  - `add_root_parm(min=, max=, strict=)`: ranges via `setMinValue/setMaxValue`/`setMinIsStrict`.
  - edge helpers / `work_status` / `anchor_graph`: read real input wires.
  - `skills/procedural-components/SKILL.md`: rewritten to the port workflow.
- **Phase 1 — agent layers** (`eee_agent/`):
  - `context_trim.py`: stub old read-back results (work_status/geometry_stats/…).
  - `loop_guard.py`: deterministic repetition guard (set_vex/delete thrash; soft@3 hard@5).
  - `tool_error_trace.py`: mark Phoenix spans ERROR on `{ok:false}`.
  - `compact_conversation` on-demand tool (shared `StateBackend`).
  - `system_prompt.py`: dropped work_status-every-step; port workflow; less todo churn.
  - `workflow_middleware.py`: default OFF (per-turn system-prompt mutation broke caching).
  - `config.py`: `EEE_RECURSION_LIMIT` default 120→999.
  - `houdini_side/chat_panel.py`: dark Houdini-native panel (earlier session).
- Verified: `py_compile` all; hython end-to-end port-flow (multi-output/wire/merge/ranges);
  `build_agent()` → `CompiledStateGraph` with all middleware.

**Pending / next session:**
- **Real end-to-end agent run NOT yet done** — it was deferred (needs the Houdini RPC
  bridge up, which must be started inside Houdini). Run on the new machine:
  start the bridge in Houdini (EEE Agent menu → Start RPC, or
  `import start_rpc; start_rpc.start()`), then
  `.\.venv\Scripts\python.exe -m eee_agent.cli prompt "build a parametric table ..."`
  with `EEE_TRACING=phoenix` and compare the new Phoenix trace to the chair-build
  baseline (expect: work_status count way down, no delete-loop, flatter token curve,
  `errorCount` reflecting real errors).
- **Subagents per component** (B2 in this plan): not implemented — largest change;
  current measures handle the immediate problems. Consider after the Claude swap.
- **Model swap to Claude** (`EEE_LLM_PROVIDER=anthropic`): still the single highest-
  reliability lever; these agent-layer fixes help any model.

---

Authored 2026-07-12. Grounded in the Phoenix trace of the latest chair-build run
(`…8a7dacd`, 424 spans / 344 s / 63 LLM calls / 148 tool calls — it **succeeded**
but inefficiently) + deepagents/LangChain docs + mainstream agent-design research.

## 1. Problem summary (evidence)
- **Context bloat is the dominant cost.** 1,802,950 tokens for one chair; prompt
  grows **monotonically 14,774 → 41,655** across 63 LLM calls (no compression);
  project-wide **prompt:completion = 83.5:1**. Latency tracks prompt size.
- **`work_status` called 64× / 148 tools (43%)**, each dumping the full param list.
- **`write_todos` 7×**, each re-emitting the entire todo list.
- **Build-delete thrash persists**: `set_vex` ×6 + `delete_node` ×6 — now driven
  by *anchor-approach* exploration (VEX point-gen → wrong point counts → delete →
  retry with blast/delete SOP), **not** VEX syntax errors (cooks are clean).
- **Errors invisible**: a real `set_parms TypeError` came back as an OK-status tool
  result → Phoenix `errorCount = 0`.

## 2. First principles
1. **Context is the scarcest resource.** Every prompt token is paid in latency,
   cost, and attention. 83:1 prompt:completion means we mostly re-read our history.
2. **Not all tool results deserve retention.** Durable facts (node paths, param
   structure, anchor wiring) must persist; stale read-backs (old `geometry_stats`,
   old `work_status`) are noise — and `work_status`/`anchor_graph` can **re-derive**
   current state from Houdini on demand, so old snapshots are never needed.
3. **The work-container graph is the source of truth, not the conversation.**
4. **Components are natural isolation boundaries** (matches our Phase-C architecture).
5. **Loops must be caught deterministically**, not by hoping the model obeys a
   prompt. Research is unanimous: external sliding-window repetition detection.
6. **Errors must be observable** to be fixable.

## 3. Root cause: why deepagents' built-in compression didn't fire
`create_deep_agent` ships `SummarizationMiddleware` + offloading — **active in our
stack** (our custom middleware is appended to the default stack). But neither fired:
- **Offloading** triggers only when a **single** tool result > **20,000 tokens**.
  Our results are each a few hundred tokens → never triggers. Our failure mode is
  the *opposite* of what offloading handles: **many small, mostly-redundant results**.
- **Summarization** triggers at **85% of the model's `max_input_tokens`** (fallback
  **170,000** if the profile is missing). We peaked at ~42k and succeeded → under
  the bar on every profile. It's a window-overflow **safety net**, not an efficiency
  mechanism; it does nothing for a run that completes at 42k that is still wasteful.

**Implication:** the built-in knobs address *few-huge* and *about-to-overflow*.
Our problem is *many-small-redundant + cumulative*. We must reduce at the source,
trim re-derivable read-backs, and isolate per component — plus re-tune the trigger.

## 4. The plan (layers, ordered by ROI)

### Layer A — Stop the bleeding at the source (quick wins, low risk)
- **A1. Kill the `work_status` double-dip.** Today the model is told to call
  `work_status` "after each step"/"to stay oriented" (`system_prompt.py:77` +
  `skills/procedural-components/SKILL.md`) **and** `WorkflowStatusMiddleware`
  injects a snapshot into the system message **every turn**. Remove the "call it
  every step" nudges; make status **on-demand only**. The middleware's per-turn
  injection is also the next item's problem.
- **A2. Stop mutating the system prompt every turn (cache + token win).**
  `WorkflowStatusMiddleware._append` changes the system message each call →
  **defeats prompt caching on every provider** (Anthropic/DeepSeek both need a
  stable prefix) and re-sends growing status text. Redesign: keep the system
  prompt **stable/cacheable**; if a status hint is wanted, put a *minimal, rarely-
  changing* summary (work path + component count) in the prompt and full detail
  via on-demand `work_status`. (Unifies A1 + B3.)
- **A3. Reduce `write_todos` churn.** Prompt the model to update todos at **phase
  boundaries** (per component / per workflow stage), not after every tool.

### Layer B — Use deepagents' mechanisms as designed
- **B1. Make summarization engage at our scale.** (a) Add the on-demand
  `compact_conversation` tool via `create_summarization_tool_middleware` so the
  agent compacts between components; (b) **verify the DeepSeek model profile** so
  the 85% threshold is computed against the real window — if langchain can't see
  it, the 170k fallback means summarization never helps; consider a tighter
  `max_input_tokens` so it trips earlier (e.g. ~32k).
- **B2. Subagents per component (the big architectural lever).** Define a
  `component-builder` subagent (`subagents=`) that builds **one** component in a
  fresh context and returns a compact report (node paths, `OUT_geo`, anchors,
  exposed params). Main agent orchestrates: `ensure_work_container` →
  `add_root_parm`s → for each component: **delegate** → `wire_anchor`/`assemble`
  → export. Each component's 20–30 tool calls stay isolated → turns one 42k
  growing context into N small ones. First-principles fit with Phase-C components.
- **B3. Prompt caching.** Automatic for Anthropic/Bedrock; for the DeepSeek
  (OpenAI-compatible) path, verify prefix-cache hits (DeepSeek supports context
  caching) — and **A2 is a prerequisite** (a mutating system prompt busts caching
  everywhere).

### Layer C — Deterministic guardrails (don't trust the model to self-police)
- **C1. Repetition/loop guardrail middleware.** Deterministic sliding-window over
  recent tool calls: trip on (tool, normalized-args) repeated ≥ N, or on the
  pathological `delete→create`-same-node / `set_vex→delete→set_vex` cycle. On trip:
  inject a "you appear stuck — change approach or STOP" directive, then hard-stop
  on recurrence. This is the research-recommended external guardrail and catches
  the anchor thrash class generically.
- **C2. Tie the step budget to the guardrail.** Keep a high cap for capable models
  but have C1 trip well before it; consider per-provider defaults (lower for
  DeepSeek, which over-iterates). The flat 999 is fine *with* C1, risky *without*.
- **C3. Bake the converged anchor recipe into the skill.** Put the working
  blast/delete-SOP anchor workflow (what the agent eventually reached) into
  `skills/procedural-components` so it stops exploring the VEX-point-gen dead-end.

### Layer D — Observability (can't fix what you can't see)
- **D1. Surface tool errors as span errors.** When a tool result is
  `{"ok": false, …}` or contains an error string, record an OTel exception/event
  so Phoenix `errorCount` reflects reality and failures are filterable.
- **D2. Structured span attrs + run metrics.** Tag tool spans with a clean
  `tool.name` attribute; record per-run token/step/tool-histogram summary so
  regressions show without ad-hoc GraphQL. (The analysis I just ran can become a
  small `eval/inspect_trace.py`.)

### Layer E — Model lever (acknowledged, user's call)
- **E1.** CLAUDE.md already notes DeepSeek over-iterates; Claude is the recommended
  swap (`EEE_LLM_PROVIDER=anthropic`) — automatic caching, stronger long-horizon +
  instruction-following (so A/C land harder). Layers A–D help **any** model; E1 is
  the single highest-reliability change.

## 5. Sequencing (effort × ROI)
| Phase | Items | Effort | Why first |
|---|---|---|---|
| **P1 quick wins** | A1, A2, A3, D1 | S–M | Small edits, biggest trace-level signal; unblocks caching |
| **P2 trim + compact** | B1, (+ read-back trim middleware) | M | Directly attacks the 1.8M-token curve |
| **P3 guardrail** | C1, C2, C3 | M | Prevents the loop class deterministically |
| **P4 architecture** | B2 subagents | L | Largest win + largest change; do after P1–P3 stable |
| Cross | B3, D2, E1 | S | Caching, metrics, model swap |

## 6. Verification (measure, don't assert)
Re-run the same chair prompt with `EEE_TRACING=phoenix` after each phase and compare:
- **Token curve**: prompt-per-LLM-call should plateau/compact, not grow 15k→42k.
- **Tool histogram**: `work_status` 64→<10; `write_todos` 7→2–3; `delete_node` 6→0–1.
- **`errorCount`** reflects real tool errors (D1).
- **Latency / total tokens** per build drop materially.
- Loop guardrail trips logged when a thrash pattern is forced.
Pull via Phoenix GraphQL (`getProjectByName:"eee-agent"`, `sort:{col:startTime,dir:desc}`,
span fields `spanKind/tokenCountPrompt/output/attributes`) — see
[[houdini-pyside6-headless-test]]-style helper if we add `eval/inspect_trace.py`.

## 7. To verify before coding
- DeepSeek model profile as seen by langchain (`max_input_tokens`) — determines
  whether B1(b) is needed. Don't assert from memory.
- Whether `WorkflowStatusMiddleware` is net-positive once caching matters —
  measure A2 with/without.
- deepagents version's exact `subagents=` API shape (declarative spec) before B2.

## Sources
- [Deep Agents overview — LangChain Docs](https://docs.langchain.com/oss/python/deepagents/overview)
- [Context engineering in Deep Agents — LangChain Docs](https://docs.langchain.com/oss/python/deepagents/context-engineering)
- [deepagents API reference — LangChain](https://reference.langchain.com/python/deepagents)
- [langchain-ai/deepagents — GitHub](https://github.com/langchain-ai/deepagents)
- [When Agents Do Not Stop: Infinite Agentic Loops — arXiv](https://arxiv.org/html/2607.01641v1)
- [Why AI Agents Get Stuck in Loops, and How to Prevent It — FixBrokenAIApps](https://www.fixbrokenaiapps.com/blog/ai-agents-infinite-loops)
- [Trim/summarize messages before invoking — LangChain Forum](https://forum.langchain.com/t/how-do-i-trim-messages-stored-in-memory-before-invoking/227)
