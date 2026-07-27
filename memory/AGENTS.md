# Project Conventions (always loaded into the agent)

The authoritative project context is `CLAUDE.md` plus the current handoff
(`docs/handoffs/2026-07-24-sandbox-verify-commit-handoff.md`). This file holds
only the conventions an agent must not violate between sessions.

## Retired tool system (do not use)

The legacy raw-write tool bridge is gone: `eee_agent.tools`, `eee_agent.bridge`,
rpyc, and `houdini_side/start_rpc.py` / `chat_panel.py` / `launch.py` were
removed. Tools like `scene_reset`, `export_geometry`, `save_hip`,
`ensure_work_container`, `make_component`, `wire_anchor`, and `assemble_output`
no longer exist — never instruct a user or model to call them, and never
suggest restarting `start_rpc.py`.

## Current production path

- Scene effects go through the **sandbox + verify + commit** workflow:
  the agent builds iteratively in an isolated sandbox container
  (`/obj/eee_scratch_<run>`, via `scratch_build`), observes cooked results,
  then promotes verified geometry into the real scene through hard gates
  (`scratch_commit` → bake / structure / orientation / health). There is no
  direct write/save/export tool.
- The legacy `propose_modeling` tool (blind-whole-spec-at-once) is retired
  from the agent graph; its module and tests are retained pending the new
  workflow stabilizing. The ChangeSet/ownership/recovery internals it used
  are kept as the commit persistence seam.
- Scene queries use the read-only tools (`scene_status`, `query_scene`,
  `inspect_workspace`, `geometry_stats`, `work_status`) over the
  authenticated loopback Secure Bridge; they return bounded plain dicts, never
  live HOM objects.
- The procedural-modeling pipeline (`skills/procedural-modeling`) adds two
  bounded tools registered on the modeling graph: `render_sketch` (Three.js
  HTML → headless-Chrome PNG for the user review gate, provider seam
  `SketchRenderProvider` in `runtime/agent_context.py`, implementation
  `eee_agent/sketch/chrome.py`) and `verify_geometry` (in-loop geometry
  assertions reusing `eval/geometry_assertions.py`). Project skills are an
  exact allowlist in `eee_agent/knowledge/sources.py`
  (`REQUIRED_SKILL_PATHS`, currently six) — adding a SKILL.md without
  registering it there fails the knowledge build by design.
- Houdini-side code (`houdini_side/`) uses only Houdini's bundled Python and
  PySide6 — no agent venv deps, no provider SDKs, no credentials.

## Verification is mandatory

- Default gate: `uv run --frozen --extra eval pytest -q` (offline; no live LLM
  or Houdini required) plus `uv lock --check`, compileall, and `git diff
  --check`.
- Real-Houdini and real-provider checks are explicit opt-in smokes/acceptance
  runners, never part of the offline gate.
- Runtime state (`app.sqlite*`, `checkpoints.sqlite*`, `runtime.token`,
  `runtime.json`, `runtime.lock`, logs, `.env`, `.venv`) is machine-local and
  must never be committed.
