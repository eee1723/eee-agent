from pathlib import Path


def test_env_example_documents_strict_deepseek_transport() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "https://api.deepseek.com/anthropic" in text
    assert "EEE_LLM_THINKING=enabled" in text
    assert "EEE_LLM_EFFORT=max" in text
    assert "EEE_LLM_MAX_TOKENS=8192" in text
    assert "deepseek-chat" in text
    assert "do not use" in text.lower()


def test_env_example_documents_knowledge_cache_options() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "EEE_KB_ENABLED" in text
    assert "EEE_KB_PATH" in text
    assert "EEE_HFS" in text


def test_gitignore_excludes_local_knowledge_cache() -> None:
    text = Path(".gitignore").read_text(encoding="utf-8")
    assert ".knowledge-cache/" in text
