import dataclasses

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from eee_agent.core.errors import AgentException
from eee_agent.providers.contracts import (
    ModelCapabilities,
    ModelProfile,
    ProviderConnection,
    ProviderKind,
    ResolvedModel,
    ThinkingEffort,
    Transport,
)
from eee_agent.providers.registry import ProviderRegistry
from eee_agent.providers.secrets import resolve_secret


class FakeAdapter:
    kind = ProviderKind.DEEPSEEK

    def build(self, connection: ProviderConnection, profile: ModelProfile):
        return FakeListChatModel(responses=[profile.model_name])


def connection() -> ProviderConnection:
    return ProviderConnection(
        connection_id="deepseek-main",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url="https://api.deepseek.com/anthropic",
        secret_ref="env:DEEPSEEK_API_KEY",
    )


def profile() -> ModelProfile:
    return ModelProfile(
        profile_id="primary",
        connection_id="deepseek-main",
        model_name="deepseek-v4-pro",
        capabilities=ModelCapabilities(
            streaming=True,
            thinking=True,
            tool_calling=True,
            structured_output=True,
            image_input=False,
        ),
        thinking_enabled=True,
        effort=ThinkingEffort.MAX,
        max_output_tokens=8192,
    )


def test_registry_resolves_adapter_and_frozen_snapshot() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())

    resolved = registry.resolve(connection(), profile())

    assert resolved.connection == connection()
    assert resolved.profile == profile()
    assert isinstance(resolved.model, FakeListChatModel)


def test_registry_rejects_duplicate_adapter() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeAdapter())


def test_registry_rejects_profile_connection_mismatch() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    wrong = ModelProfile(
        profile_id="wrong",
        connection_id="another-connection",
        model_name="deepseek-v4-pro",
        capabilities=profile().capabilities,
    )

    with pytest.raises(ValueError, match="connection_id"):
        registry.resolve(connection(), wrong)


def test_resolve_secret_rejects_unsupported_prefix() -> None:
    with pytest.raises(AgentException) as exc:
        resolve_secret("vault:deeepseek")
    assert exc.value.error.code == "provider.unsupported_secret_ref"


def test_resolve_secret_raises_when_env_var_unset(monkeypatch) -> None:
    monkeypatch.delenv("EEE_TEST_KEY", raising=False)
    with pytest.raises(AgentException) as exc:
        resolve_secret("env:EEE_TEST_KEY")
    assert exc.value.error.code == "provider.missing_key"
    assert exc.value.error.requires_user_action is True


def test_resolve_secret_raises_when_env_var_empty(monkeypatch) -> None:
    monkeypatch.setenv("EEE_TEST_KEY", "")
    with pytest.raises(AgentException) as exc:
        resolve_secret("env:EEE_TEST_KEY")
    assert exc.value.error.code == "provider.missing_key"


def test_resolve_secret_returns_value_when_env_var_set(monkeypatch) -> None:
    monkeypatch.setenv("EEE_TEST_KEY", "unit-test-key")
    assert resolve_secret("env:EEE_TEST_KEY") == "unit-test-key"


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("timeout_seconds", 0, "must be positive"),
        ("timeout_seconds", -1, "must be positive"),
        ("max_retries", -1, "must be non-negative"),
        ("connection_id", "", "must not be empty"),
        ("connection_id", "   ", "must not be empty"),
    ],
)
def test_provider_connection_rejects_invalid_bounds(field, value, match) -> None:
    kwargs = dict(
        connection_id="deepseek-main",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url="https://api.deepseek.com/anthropic",
        secret_ref="env:DEEPSEEK_API_KEY",
    )
    kwargs[field] = value
    with pytest.raises(ValueError, match=match):
        ProviderConnection(**kwargs)


def test_provider_connection_allows_zero_max_retries() -> None:
    conn = ProviderConnection(
        connection_id="deepseek-main",
        provider=ProviderKind.DEEPSEEK,
        transport=Transport.ANTHROPIC,
        base_url="https://api.deepseek.com/anthropic",
        secret_ref="env:DEEPSEEK_API_KEY",
        max_retries=0,
    )
    assert conn.max_retries == 0


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("profile_id", "", "must not be empty"),
        ("profile_id", "  ", "must not be empty"),
        ("model_name", "", "must not be empty"),
        ("model_name", "\t", "must not be empty"),
        ("max_output_tokens", 0, "must be positive"),
    ],
)
def test_model_profile_rejects_invalid_bounds(field, value, match) -> None:
    kwargs = dict(
        profile_id="primary",
        connection_id="deepseek-main",
        model_name="deepseek-v4-pro",
        capabilities=ModelCapabilities(
            streaming=True,
            thinking=True,
            tool_calling=True,
            structured_output=True,
            image_input=False,
        ),
    )
    kwargs[field] = value
    with pytest.raises(ValueError, match=match):
        ModelProfile(**kwargs)


def test_model_profile_rejects_thinking_without_capability() -> None:
    with pytest.raises(ValueError, match="thinking cannot be enabled"):
        ModelProfile(
            profile_id="primary",
            connection_id="deepseek-main",
            model_name="deepseek-v4-pro",
            capabilities=ModelCapabilities(
                streaming=True,
                thinking=False,
                tool_calling=True,
                structured_output=True,
                image_input=False,
            ),
            thinking_enabled=True,
        )

def test_resolved_value_objects_are_frozen_and_slotted() -> None:
    registry = ProviderRegistry()
    registry.register(FakeAdapter())
    resolved = registry.resolve(connection(), profile())

    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.connection = connection()
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.profile.profile_id = "x"

    assert ModelProfile.__dataclass_params__.frozen is True
    assert hasattr(ModelProfile, "__slots__")
    assert ResolvedModel.__dataclass_params__.frozen is True
    assert hasattr(ResolvedModel, "__slots__")


def test_registry_raises_when_no_adapter_registered() -> None:
    registry = ProviderRegistry()
    with pytest.raises(ValueError, match="no provider adapter"):
        registry.resolve(connection(), profile())
