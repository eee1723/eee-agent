"""Provider-neutral model factory."""
from __future__ import annotations

from langchain_core.language_models import BaseChatModel

from eee_agent.config import llm_config
from eee_agent.providers.factory import resolve_model


def build_model() -> BaseChatModel:
    return resolve_model(llm_config()).model
