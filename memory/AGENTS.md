# Project Conventions (always loaded into the agent)

The authoritative project context is `CLAUDE.md` plus the current handoff
(`docs/handoffs/2026-07-20-runtime-development-transfer.md`). This file holds
only the conventions an agent must not violate between sessions.

## Retired tool system (do not use)

The legacy raw-write tool bridge is gone: `eee_agent.tools`, `eee_agent.bridge`,
rpyc, and `houdini_side/start_rpc.py` / `chat_panel.py` / `launch.py` were
removed. Tools like `scene_reset`, `export_geometry`, `save_hip`,
`ensure_work_container`, `make_component`, `wire_anchor`, and `assemble_output`
no longer exist — never instruct a user or model to call them, and never
suggest restarting `start_rpc.py`.

## Current production path

- Scene effects go only through the persistent Runtime: typed proposal
  (`propose_modeling` with a strict Brief/Spec payload) → explicit approval →
  transactional Apply with receipt. There is no direct write/save/export tool.
- Scene queries use exactly five read-only tools (`scene_status`,
  `query_scene`, `inspect_workspace`, `geometry_stats`, `work_status`) over the
  authenticated loopback Secure Bridge; they return bounded plain dicts, never
  live HOM objects.
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
