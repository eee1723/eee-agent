"""Multi-provider model factory. Default: DeepSeek V4 Pro (OpenAI-compatible).

Verified 2026-07-11: DeepSeek API is OpenAI-compatible; model id is
``deepseek-v4-pro``; base_url ``https://api.deepseek.com``. Legacy names
deepseek-chat / deepseek-reasoner are deprecated 2026/07/24 — not used.
"""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from eee_agent.config import llm_config

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def build_model() -> BaseChatModel:
    cfg = llm_config()

    if cfg.provider == "deepseek":
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set (see .env)")
        return ChatOpenAI(model=cfg.model, base_url=DEEPSEEK_BASE_URL, api_key=api_key)

    if cfg.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=cfg.model, api_key=os.getenv("ANTHROPIC_API_KEY") or None
        )

    if cfg.provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=cfg.model, api_key=os.getenv("OPENAI_API_KEY") or None)

    raise ValueError(f"unknown EEE_LLM_PROVIDER: {cfg.provider!r}")
