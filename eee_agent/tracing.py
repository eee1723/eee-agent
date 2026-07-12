"""Optional OpenTelemetry tracing to a local Arize Phoenix server (no Docker).

Enable by setting ``EEE_TRACING=phoenix`` and starting Phoenix first:
    python -m phoenix.server.main serve        # UI + OTLP at http://localhost:6006

OpenInference auto-instruments LangChain/LangGraph (deepagents is built on them),
so every model call, tool call, and graph step becomes a span. Use the Phoenix UI
to SEE where the agent loops. To swap to Langfuse later, only the OTLP endpoint
(``PHOENIX_OTLP_ENDPOINT``) changes — the instrumentation stays identical.
"""
from __future__ import annotations

import os

_started = False


def setup_tracing() -> bool:
    """Instrument LangChain/LangGraph for Phoenix if EEE_TRACING=phoenix.

    Idempotent. Returns True if tracing is active. Safe to call even when the
    Phoenix server is not yet running (instrument() doesn't connect; spans are
    exported on emission, so start Phoenix before running the agent).
    """
    global _started
    if _started:
        return True

    provider = os.getenv("EEE_TRACING", "").strip().lower()
    if provider != "phoenix":
        return False

    from openinference.instrumentation.langchain import LangChainInstrumentor
    from phoenix.otel import register

    endpoint = os.getenv("PHOENIX_OTLP_ENDPOINT", "http://localhost:6006/v1/traces")
    project = os.getenv("EEE_TRACING_PROJECT", "eee-agent")
    # batch=False (default) -> SimpleSpanProcessor: each span is exported at once,
    # so you can watch the trace build up live and won't lose it if the run is killed.
    tracer_provider = register(project_name=project, endpoint=endpoint)
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
    _started = True
    print(f"[eee] tracing -> Phoenix {endpoint} (project={project})", flush=True)
    return True
