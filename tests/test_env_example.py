from pathlib import Path


def test_env_example_documents_strict_deepseek_transport() -> None:
    text = Path(".env.example").read_text(encoding="utf-8")
    assert "https://api.deepseek.com/anthropic" in text
    assert "EEE_LLM_THINKING=enabled" in text
    assert "EEE_LLM_EFFORT=max" in text
    assert "EEE_LLM_MAX_TOKENS=8192" in text
    assert "deepseek-chat" in text
    assert "do not use" in text.lower()
