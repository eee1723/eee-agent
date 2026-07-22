from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
import pytest

from eee_agent.config import _env_bool, llm_config, vision_config
from eee_agent.core.errors import AgentException
from eee_agent.model import build_model


def clear_model_env(monkeypatch) -> None:
    for name in (
        "EEE_LLM_PROVIDER",
        "EEE_LLM_MODEL",
        "EEE_LLM_THINKING",
        "EEE_LLM_EFFORT",
        "EEE_LLM_MAX_TOKENS",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def clear_vision_env(monkeypatch) -> None:
    for name in (
        "EEE_VISION_PROVIDER",
        "EEE_VISION_MODEL",
        "EEE_VISION_MAX_IMAGE_BYTES",
        "EEE_VISION_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_config_selects_strict_deepseek_v4(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    config = llm_config()
    assert config.provider == "deepseek"
    assert config.model == "deepseek-v4-pro"
    assert config.thinking_enabled is True
    assert config.effort == "max"
    assert config.max_output_tokens == 8192


def test_build_model_uses_chat_anthropic_for_deepseek(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = build_model()
    assert isinstance(model, ChatAnthropic)
    assert model.anthropic_api_url == "https://api.deepseek.com/anthropic"


def test_build_model_preserves_standard_anthropic(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-key")
    assert isinstance(build_model(), ChatAnthropic)


def test_build_model_preserves_standard_openai(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("EEE_LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    assert isinstance(build_model(), ChatOpenAI)


def test_deepseek_alias_is_rejected_before_any_request(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_LLM_MODEL", "deepseek-reasoner")
    with pytest.raises(AgentException) as caught:
        build_model()
    assert caught.value.error.code == "provider.invalid_model"


# --- Finding #1: config validation coverage ---------------------------------


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "enabled", "TRUE", " on "]
)
def test_env_bool_accepts_truthy_synonyms(monkeypatch, value: str) -> None:
    monkeypatch.setenv("EEE_BOOL_UNDER_TEST", value)
    assert _env_bool("EEE_BOOL_UNDER_TEST", False) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "disabled"])
def test_env_bool_accepts_falsy_synonyms(monkeypatch, value: str) -> None:
    monkeypatch.setenv("EEE_BOOL_UNDER_TEST", value)
    assert _env_bool("EEE_BOOL_UNDER_TEST", True) is False


def test_env_bool_rejects_garbage_value(monkeypatch) -> None:
    monkeypatch.setenv("EEE_BOOL_UNDER_TEST", "maybe")
    with pytest.raises(ValueError):
        _env_bool("EEE_BOOL_UNDER_TEST", False)


def test_env_bool_defaults_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("EEE_BOOL_UNDER_TEST", raising=False)
    assert _env_bool("EEE_BOOL_UNDER_TEST", True) is True
    assert _env_bool("EEE_BOOL_UNDER_TEST", False) is False


def test_unknown_provider_raises(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "grok")
    with pytest.raises(ValueError):
        llm_config()


def test_effort_invalid_value_raises(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_EFFORT", "medium")
    with pytest.raises(ValueError):
        llm_config()


def test_effort_is_normalized_stripped_and_lowercased(monkeypatch) -> None:
    # Guards Fix #4: " high " must be accepted and normalized to "high".
    # Use a non-deepseek provider so the default effort is None, isolating
    # the env-supplied value.
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_EFFORT", " high ")
    assert llm_config().effort == "high"


def test_effort_high(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_EFFORT", "high")
    assert llm_config().effort == "high"


def test_effort_empty_string_falls_back_to_default(monkeypatch) -> None:
    # Guards the "empty-string-falls-back-to-default" behavior of Fix #4.
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_EFFORT", "   ")
    assert llm_config().effort is None


def test_max_tokens_non_integer_raises(monkeypatch) -> None:
    # Guards Fix #5: non-integer must raise the clear message.
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_MAX_TOKENS", "abc")
    with pytest.raises(ValueError, match="must be an integer"):
        llm_config()


@pytest.mark.parametrize("value", ["-1", "0"])
def test_max_tokens_non_positive_raises(monkeypatch, value: str) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_MAX_TOKENS", value)
    with pytest.raises(ValueError):
        llm_config()


def test_max_tokens_custom_value(monkeypatch) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_MAX_TOKENS", "4096")
    assert llm_config().max_output_tokens == 4096


# --- Finding #3: anthropic thinking-on wiring + max_tokens propagation -------


def test_build_model_anthropic_thinking_on_and_effort_and_max_tokens(
    monkeypatch,
) -> None:
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setenv("EEE_LLM_THINKING", "true")
    monkeypatch.setenv("EEE_LLM_EFFORT", "high")
    monkeypatch.setenv("EEE_LLM_MAX_TOKENS", "4096")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unit-test-key")
    model = build_model()
    assert isinstance(model, ChatAnthropic)
    assert model.thinking == {"type": "enabled"}
    assert model.effort == "high"
    assert model.max_tokens == 4096


def test_build_model_openai_propagates_max_tokens(monkeypatch) -> None:
    # Locks Fix #2: OpenAI adapter must forward max_tokens to ChatOpenAI.
    clear_model_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "openai")
    monkeypatch.setenv("EEE_LLM_MODEL", "gpt-4.1")
    monkeypatch.setenv("EEE_LLM_MAX_TOKENS", "1234")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-key")
    model = build_model()
    assert isinstance(model, ChatOpenAI)
    assert model.max_tokens == 1234


# --- Stage B: explicit provider-neutral Vision configuration ---------------


def test_vision_config_is_disabled_by_default(monkeypatch) -> None:
    clear_vision_env(monkeypatch)
    monkeypatch.setenv("EEE_LLM_PROVIDER", "openai")
    assert vision_config() is None


def test_vision_config_reuses_supported_provider_identifiers(monkeypatch) -> None:
    clear_vision_env(monkeypatch)
    monkeypatch.setenv("EEE_VISION_PROVIDER", " openai ")
    monkeypatch.setenv("EEE_VISION_MODEL", " gpt-4.1 ")
    config = vision_config()
    assert config is not None
    assert config.provider == "openai"
    assert config.model == "gpt-4.1"
    assert config.max_image_bytes == 8 * 1024 * 1024
    assert config.timeout_seconds == 30.0


def test_vision_config_rejects_unknown_provider(monkeypatch) -> None:
    clear_vision_env(monkeypatch)
    monkeypatch.setenv("EEE_VISION_PROVIDER", "deepseek")
    with pytest.raises(ValueError, match="EEE_VISION_PROVIDER"):
        vision_config()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("EEE_VISION_MAX_IMAGE_BYTES", "not-an-int", "must be an integer"),
        ("EEE_VISION_MAX_IMAGE_BYTES", "0", "must be between"),
        ("EEE_VISION_MAX_IMAGE_BYTES", "16777217", "must be between"),
        ("EEE_VISION_TIMEOUT_SECONDS", "not-a-float", "must be a number"),
        ("EEE_VISION_TIMEOUT_SECONDS", "0", "must be between"),
        ("EEE_VISION_TIMEOUT_SECONDS", "120.1", "must be between"),
    ],
)
def test_vision_config_rejects_invalid_bounds(
    monkeypatch, name: str, value: str, message: str
) -> None:
    clear_vision_env(monkeypatch)
    monkeypatch.setenv("EEE_VISION_PROVIDER", "anthropic")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        vision_config()


def test_vision_config_accepts_explicit_bounds(monkeypatch) -> None:
    clear_vision_env(monkeypatch)
    monkeypatch.setenv("EEE_VISION_PROVIDER", "anthropic")
    monkeypatch.setenv("EEE_VISION_MAX_IMAGE_BYTES", "16777216")
    monkeypatch.setenv("EEE_VISION_TIMEOUT_SECONDS", "0.1")
    config = vision_config()
    assert config is not None
    assert config.model == "claude-sonnet-5"
    assert config.max_image_bytes == 16_777_216
    assert config.timeout_seconds == 0.1
