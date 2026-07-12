"""ContextSeek semantic-memory integration. Opt-in via ``EEE_CONTEXTSEEK=true``.

Wires ``ContextSeekMiddleware`` into the deepagents agent:
- each model call retrieves relevant PRIOR LESSONS into the system prompt;
- the agent's final answers are stored back (compounding knowledge across runs);
- periodic ``compact()`` evolves raw items toward reusable skills.

Storage: the **file** backend at ``<repo>/.contextseek/store`` (verified to persist
across processes on Windows; the default seekdb embedded backend is Linux-only and
silently falls back to in-memory ``memory``, which loses everything per process).

Seeded once with hard-won lessons from trace analysis.
"""
from __future__ import annotations

import dataclasses
import os

from eee_agent.config import repo_root

SCOPE = "eee-houdini"
STORE_PATH = os.path.join(repo_root(), ".contextseek", "store")


def is_enabled() -> bool:
    return os.getenv("EEE_CONTEXTSEEK", "").strip().lower() == "true"


def _build_persistent_ctx(model):
    """ContextSeek client with file-backed storage + our model as summarizer."""
    from contextseek.client.contextseek import ContextSeek
    from contextseek.config.factory import build_summarizer
    from contextseek.config.settings import ContextSeekSettings

    base = ContextSeekSettings()
    storage = base.storage.model_copy(update={"backend": "file", "path": STORE_PATH})
    settings = base.model_copy(update={"storage": storage})
    ctx = ContextSeek.from_settings(settings)
    # Use the agent's own model as the summarizer (mirrors the middleware's
    # internal build), so distillation rides on the configured DeepSeek key.
    if model is not None:
        ctx = dataclasses.replace(ctx, summarizer=build_summarizer(settings.summarizer, llm=model))
    return ctx


def build_middleware(model):
    """Build the ContextSeek middleware over a persistent (file-backed) ctx."""
    from contextseek.bridges.langchain.middleware import ContextSeekMiddleware

    ctx = _build_persistent_ctx(model)
    _seed_lessons(ctx)
    return ContextSeekMiddleware(
        ctx=ctx,
        scope=SCOPE,
        retrieval_k=6,
        auto_compact=True,
        compact_every=10,
    )


# Lessons distilled from the Phoenix trace of the looping run.
_LESSONS = [
    "Houdini VEX has NO per-prim getbbox(0,@primnum,...). To iterate a prim's "
    "points use `int pts[] = primpoints(0, @primnum);`. To mark windows on a grid, "
    "run an attribwrangle over PRIMITIVES with `i@is_window = (@primnum % 2);` then "
    "a Blast node with group `@is_window==1`, grouptype=prims to cut the holes.",

    "When a wrangle cook fails, set_vex/cook_node return vex_errors naming the "
    "function and line:col (and matching-function candidates). READ it and fix "
    "that one line in place. Do NOT delete the wrangle and rewrite from scratch.",

    "Build the building ONCE per the recipe and export; do not create variant "
    "nodes (wall2, wall3, front_3d_b...) to try alternatives — refine the existing "
    "nodes in place. Aim to finish and export within ~40 tool calls.",
]


def _seed_lessons(ctx) -> None:
    """Idempotent seed via a marker file (file backend persists across runs)."""
    marker = os.path.join(repo_root(), ".contextseek_seeded")
    if os.path.isfile(marker):
        return
    for i, lesson in enumerate(_LESSONS):
        try:
            ctx.add(lesson, scope=SCOPE, source="seed", tags=["seed", "lesson"])
        except Exception as e:  # noqa: BLE001
            print(f"[eee] contextseek seed lesson {i} failed: {e}", flush=True)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("v1")
    except Exception:
        pass
