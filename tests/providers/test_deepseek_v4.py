import dataclasses

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from eee_agent.core.errors import AgentException
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.deepseek_v4 import (
    DEEPSEEK_ANTHROPIC_URL,
    DeepSeekV4ProviderAdapter,
)


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id="deepseek-official",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url=DEEPSEEK_ANTHROPIC_URL,
        secret_ref="env:DEEPSEEK_API_KEY",
    )


def profile(model_name: str = "deepseek-v4-pro") -> ModelProfile:
    return ModelProfile(
        profile_id="primary",
        connection_id="deepseek-official",
        model_name=model_name,
        capabilities=ModelCapabilities(True, True, True, True, False),
        thinking_enabled=True,
        effort=ThinkingEffort.MAX,
        max_output_tokens=8192,
    )


@pytest.mark.parametrize("model_name", ["deepseek-v4-pro", "deepseek-v4-flash"])
def test_adapter_builds_chat_anthropic_with_official_v4_settings(
    monkeypatch, model_name: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = DeepSeekV4ProviderAdapter().build(connection(), profile(model_name))
    assert isinstance(model, ChatAnthropic)
    assert model.model == model_name
    assert model.anthropic_api_url == DEEPSEEK_ANTHROPIC_URL
    assert model.thinking == {"type": "enabled"}
    assert model.effort == "max"
    assert model.output_version == "v1"
    # Pass-through assertions: connection/profile values reach the model.
    assert model.max_tokens == 8192
    assert model.max_retries == 2
    assert model.stream_usage is True


@pytest.mark.parametrize("alias", ["deepseek-chat", "deepseek-reasoner", "claude-sonnet-5"])
def test_adapter_rejects_aliases_that_could_silently_map_to_flash(
    monkeypatch, alias: str
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(connection(), profile(alias))
    assert caught.value.error.code == "provider.invalid_model"


def test_adapter_disables_thinking_when_profile_has_thinking_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    disabled_profile = dataclasses.replace(profile(), thinking_enabled=False, effort=None)
    model = DeepSeekV4ProviderAdapter().build(connection(), disabled_profile)
    assert model.thinking == {"type": "disabled"}
    assert model.effort is None


def test_adapter_ignores_effort_when_thinking_is_disabled(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    # effort is present but dormant: thinking_enabled=False must force effort=None.
    dormant_profile = dataclasses.replace(
        profile(), thinking_enabled=False, effort=ThinkingEffort.HIGH
    )
    model = DeepSeekV4ProviderAdapter().build(connection(), dormant_profile)
    assert model.thinking == {"type": "disabled"}
    assert model.effort is None


def test_adapter_rejects_non_anthropic_transport(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    conn = dataclasses.replace(connection(), transport=Transport.OPENAI)
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(conn, profile())
    assert caught.value.error.code == "provider.invalid_transport"


def test_adapter_rejects_wrong_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    conn = dataclasses.replace(connection(), base_url="https://api.deepseek.com/v1")
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(conn, profile())
    assert caught.value.error.code == "provider.invalid_endpoint"


def test_adapter_rejects_image_input_capability(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    image_caps = ModelCapabilities(True, True, True, True, True)
    image_profile = dataclasses.replace(profile(), capabilities=image_caps)
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(connection(), image_profile)
    assert caught.value.error.code == "provider.invalid_capability"


def test_adapter_normalizes_trailing_slash_in_base_url(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    conn = dataclasses.replace(connection(), base_url="https://api.deepseek.com/anthropic/")
    model = DeepSeekV4ProviderAdapter().build(conn, profile())
    assert model.anthropic_api_url == DEEPSEEK_ANTHROPIC_URL


def test_adapter_reports_missing_key_without_leaking_a_value(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AgentException) as caught:
        DeepSeekV4ProviderAdapter().build(connection(), profile())
    assert caught.value.error.code == "provider.missing_key"
    assert "unit-test-key" not in str(caught.value)


def test_anthropic_payload_replays_thinking_before_tool_result(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    model = DeepSeekV4ProviderAdapter().build(connection(), profile())
    assistant = AIMessage(
        content=[
            {"type": "thinking", "thinking": "Need the lookup tool.", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"q": "x"}},
        ]
    )
    payload = model._get_request_payload(  # noqa: SLF001 - intentional adapter contract
        [
            HumanMessage(content="Find x"),
            assistant,
            ToolMessage(content="result", tool_call_id="toolu_1"),
        ]
    )
    assistant_content = payload["messages"][1]["content"]
    assert assistant_content[0] == {
        "type": "thinking",
        "thinking": "Need the lookup tool.",
        "signature": "sig",
    }
    assert assistant_content[1]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["output_config"] == {"effort": "max"}
