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
from importlib import import_module

from eee_agent.config import repo_root

SCOPE = "eee-houdini"
STORE_PATH = os.path.join(repo_root(), ".contextseek", "store")


def is_enabled() -> bool:
    return os.getenv("EEE_CONTEXTSEEK", "").strip().lower() == "true"


def _build_persistent_ctx(model):
    """ContextSeek client with file-backed storage + our model as summarizer."""
    contextseek_client = import_module("contextseek.client.contextseek")
    contextseek_factory = import_module("contextseek.config.factory")
    contextseek_settings = import_module("contextseek.config.settings")
    contextseek_type = getattr(contextseek_client, "ContextSeek")
    build_summarizer = getattr(contextseek_factory, "build_summarizer")
    settings_type = getattr(contextseek_settings, "ContextSeekSettings")

    base = settings_type()
    storage = base.storage.model_copy(update={"backend": "file", "path": STORE_PATH})
    settings = base.model_copy(update={"storage": storage})
    ctx = contextseek_type.from_settings(settings)
    # Use the agent's own model as the summarizer (mirrors the middleware's
    # internal build), so distillation rides on the configured DeepSeek key.
    if model is not None:
        ctx = dataclasses.replace(ctx, summarizer=build_summarizer(settings.summarizer, llm=model))
    return ctx


def build_middleware(model):
    """Build the ContextSeek middleware over a persistent (file-backed) ctx."""
    contextseek_middleware = import_module(
        "contextseek.bridges.langchain.middleware"
    )
    middleware_type = getattr(contextseek_middleware, "ContextSeekMiddleware")

    ctx = _build_persistent_ctx(model)
    _seed_lessons(ctx)
    return middleware_type(
        ctx=ctx,
        scope=SCOPE,
        retrieval_k=6,
        auto_compact=True,
        compact_every=10,
    )


# Lessons distilled from trace analysis, retargeted to the
# sandbox → verify → commit workflow (scratch_build / scratch_commit).
_LESSONS = [
    "Iterate with scratch_build ONE small step at a time: add a primitive, set "
    "its parms, read the returned cook errors and geometry stats, then adjust "
    "the SAME sandbox with the next scratch_build call. Do NOT try to build the "
    "whole asset in a single call, and do NOT restart from scratch on a cook "
    "error — the error names the failing node; fix that operation.",

    "scratch_build set_parm accepts literal values only — no expressions, no "
    "ch() references, no VEX. If you need a driven relationship, bake the "
    "values yourself and set the numbers. There is no attribwrangle in the "
    "catalog; procedural logic belongs in your op sequence (copytopoints2, "
    "sweep2, boolean2, polyextrude2), not in code snippets.",

    "Only call scratch_commit AFTER scratch_build iterations produce the "
    "geometry you want. A refused commit is not an error to retry blindly: "
    "read the gates list, fix the named defect in the sandbox, then re-commit. "
    "Never use skip_structure_check to bypass a monolithic-structure failure.",
]

_SEED_VERSION = "v2"


def _seed_lessons(ctx) -> None:
    """Idempotent seed via a versioned marker file (file backend persists)."""
    marker = os.path.join(repo_root(), ".contextseek_seeded")
    try:
        if os.path.isfile(marker):
            with open(marker) as fh:
                if fh.read().strip() == _SEED_VERSION:
                    return
    except OSError:
        return
    for i, lesson in enumerate(_LESSONS):
        try:
            ctx.add(lesson, scope=SCOPE, source="seed", tags=["seed", "lesson"])
        except Exception as e:  # noqa: BLE001
            print(f"[eee] contextseek seed lesson {i} failed: {e}", flush=True)
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write(_SEED_VERSION)
    except Exception:
        pass
